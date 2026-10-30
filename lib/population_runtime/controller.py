"""World-thread policy controller; Playerbots remains the session and AI executor."""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal, ROUND_CEILING
from fractions import Fraction
from pathlib import Path

from .adapter import PopulationAdapter
from .bands import allocate_exact, classify_earned_progress
from .canonical import digest_file
from .capabilities import InstalledCapabilities, load_capabilities
from .config import RuntimeConfig, load_config
from .errors import ArtifactError, BridgeError, PendingOperation
from .manifest import IdentityManifest, load_manifest
from .models import BandAssignment, Identity
from .state import RuntimeState, append_audit, load_state, save_state


class PopulationController:
    """Deterministic policy with bounded reads/actions and fail-closed reconciliation."""

    def __init__(
        self,
        config: RuntimeConfig,
        capabilities: InstalledCapabilities,
        manifest: IdentityManifest,
        state: RuntimeState,
        adapter: PopulationAdapter,
        clock: Callable[[], int],
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self.manifest = manifest
        self.state = state
        self.adapter = adapter
        self.clock = clock
        self._recovery_pending = True
        self._recovery_keys = deque(sorted(state.admitted_identity_keys))
        self._last_tick_epoch = 0
        self._last_release_key = ""
        self._identity_by_key = {item.identity_key: item for item in manifest.identities}
        self._account_slots: dict[str, int] = {}
        for item in manifest.identities:
            self._account_slots[item.account_key] = self._account_slots.get(item.account_key, 0) + 1
        if len(self._identity_by_key) != config.durable_target:
            raise ArtifactError("identity manifest contains duplicate keys")
        known = self._identity_by_key.keys()
        if state.pending_admission_key is not None:
            if state.pending_admission_keys:
                raise ArtifactError("ambiguous pending admission state")
            state.pending_admission_keys = [state.pending_admission_key]
            state.pending_admission_key = None
        if (len(set(state.pending_admission_keys)) != len(state.pending_admission_keys)
            or not set(state.pending_admission_keys) <= known
            or set(state.pending_admission_keys) & state.admitted_identity_keys):
            raise ArtifactError("pending admission window is outside the manifest, duplicated, or already admitted")
        # A crash after native admission is reconciled through the same exact CAS path.
        for field, values in (
            ("bindings", state.bindings.keys()),
            ("admitted identities", state.admitted_identity_keys),
            ("progress cache", state.progress_cache.keys()),
            ("desired sessions", state.desired_session_keys),
        ):
            if not set(values) <= known:
                raise ArtifactError(f"runtime state {field} contains identities outside the manifest")
        self._artifact_digests = {
            path: digest_file(path)
            for path in (
                config.manifest_path,
                config.capabilities_path,
                config.identity_gate_path,
                config.decisions_path,
                config.schedule_path,
                config.template_catalogue_path,
                config.appearance_catalogue_path,
            )
        }

        self._artifact_digests.update(manifest.eligibility_artifacts)
        if config.newcomer_schedule_path is not None:
            self._artifact_digests[config.newcomer_schedule_path] = digest_file(config.newcomer_schedule_path)

    @classmethod
    def from_config_path(
        cls,
        path: Path,
        adapter: PopulationAdapter,
        clock: Callable[[], int],
    ) -> PopulationController:
        config = load_config(path)
        capabilities = load_capabilities(
            config.capabilities_path, config.capabilities_file_sha256
        )
        manifest = load_manifest(config)
        state = load_state(
            config.state_path,
            manifest.manifest_digest,
            capabilities.manifest_digest,
            capabilities.generation,
        )
        controller = cls(config, capabilities, manifest, state, adapter, clock)
        controller._artifact_digests[path] = digest_file(path)
        return controller

    def _artifacts_unchanged(self) -> bool:
        try:
            return all(path.is_file() and not path.is_symlink() and digest_file(path) == expected
                       for path, expected in self._artifact_digests.items())
        except OSError:
            return False

    def _fail_closed(self, reason: str, now: int) -> bool:
        if not self.adapter.population_fail_closed():
            raise BridgeError(f"native bridge rejected fail-closed scheduler shutdown: {reason}")
        self.state.desired_session_keys.clear()
        # Revocation is immediate; native cleanup clears each ledger row only on ACK.
        # Keep observed session flags until a fresh authoritative snapshot replaces them.
        self.state.revision += 1
        save_state(self.config.state_path, self.state)
        append_audit(
            self.config.audit_path,
            {"epoch": now, "revision": self.state.revision, "result": "fail_closed", "reason": reason},
        )
        return False

    def _fresh(self, observed_epoch: int, now: int, name: str) -> None:
        if observed_epoch > now or now - observed_epoch > self.config.snapshot_max_age_seconds:
            raise BridgeError(f"{name} snapshot is stale or from the future")

    def _starter_pressure(self) -> bool:
        counts: dict[int, int] = {}
        for record in self.state.progress_cache.values():
            if record.online and record.zone_id in self.config.starter_zone_ids:
                counts[record.zone_id] = counts.get(record.zone_id, 0) + 1
        return any(count >= self.config.starter_zone_online_cap for count in counts.values())

    def _bind_and_admit(self, identity: Identity) -> None:
        saved = self.state.bindings.get(identity.identity_key)
        if identity.identity_key in self.state.admitted_identity_keys and saved is None:
            raise BridgeError(f"saved admission lacks binding for {identity.identity_key}")
        if not self.adapter.population_reserve_identity(
            identity.identity_key,
            identity.account_key,
            identity.guid_allocation_key,
            identity.wave_key,
        ):
            raise BridgeError(f"identity reservation was rejected for {identity.identity_key}")
        account_id = self.adapter.population_ensure_account(
            identity.account_key,
            identity.account_name,
            self._account_slots[identity.account_key],
        )
        if account_id <= 0:
            raise BridgeError(f"account readback was invalid for {identity.identity_key}")
        if saved is not None and saved.account_id != account_id:
            raise BridgeError(f"saved account changed for {identity.identity_key}")
        character_guid = self.adapter.population_ensure_character(
            identity.identity_key, account_id, identity.immutable_creation_fields
        )
        if character_guid <= 0:
            raise BridgeError(f"character readback was invalid for {identity.identity_key}")
        if saved is not None and saved.character_guid != character_guid:
            raise BridgeError(f"saved character changed for {identity.identity_key}")
        binding = self.adapter.population_bind_identity_once(
            identity.identity_key,
            account_id,
            character_guid,
            self.manifest.manifest_digest,
        )
        if (
            binding.identity_key != identity.identity_key
            or binding.account_id != account_id
            or binding.character_guid != character_guid
        ):
            raise BridgeError(f"identity CAS readback mismatch for {identity.identity_key}")
        if not self.adapter.population_set_admission(
            identity.identity_key, identity.wave_key, self.manifest.manifest_digest
        ):
            raise BridgeError(f"admission CAS was rejected for {identity.identity_key}")
        self.state.bindings[identity.identity_key] = binding
        self.state.admitted_identity_keys.add(identity.identity_key)

    def _admit_due_batch(self, owner_level: int, now: int, admission_slots: int) -> list[str]:
        pending = self.state.pending_admission_keys
        limit = min(self.config.admission_batch_size, self.config.action_batch_size)
        cadence_ready = (not self.state.last_admission_epoch or
                         now - self.state.last_admission_epoch >= self.config.admission_interval_seconds)
        # Count pending reservations as capacity even before native acknowledgement.
        occupied = self.state.admitted_identity_keys | set(pending)
        room = min(max(0, admission_slots), max(0, self.config.pending_admission_limit - len(pending)),
                   max(0, self.config.admission_capacity - len(occupied)), limit)
        added = []
        if cadence_ready and room:
            eligible = (self.manifest.identities[:sum(self.config.newcomer_wave_counts[:owner_level])]
                        if self.config.newcomer_wave_counts is not None else self.manifest.identities)
            for item in eligible:
                if (item.identity_key not in occupied and item.identity_key not in self.manifest.admission_holds
                    and item.identity_key not in self.manifest.legacy_identity_keys
                    and (self.config.newcomer_wave_counts is not None or item.wave_owner_level <= owner_level)):
                    added.append(item.identity_key)
                    if len(added) == room:
                        break
        if added:
            pending.extend(added)
            self.state.last_admission_epoch = now
            # One durable batch intent precedes EVERY native reservation or creation.
            save_state(self.config.state_path, self.state)
        admitted = []
        for key in list(pending[:limit]):
            try:
                self._bind_and_admit(self._identity_by_key[key])
            except PendingOperation:
                # Bounded round robin; slow commits cannot starve other identities.
                pending.remove(key)
                pending.append(key)
                continue
            pending.remove(key)
            admitted.append(key)
        return admitted

    def _refresh_progress(self) -> None:
        admitted = sorted(self.state.admitted_identity_keys)
        if not admitted:
            return
        active, uncached, offline = [], [], []
        for key in admitted:
            record = self.state.progress_cache.get(key)
            if key in self.state.desired_session_keys or (record is not None and (record.online or record.session_requested)):
                active.append(key)
            elif record is None:
                uncached.append(key)
            else:
                offline.append(key)
        # One persisted ticket gives each nonempty group its configured share.
        # No uncommitted pending identity is read: all groups are admitted keys.
        groups = [(keys, weight) for keys, weight in zip(
            (active, uncached, offline), self.config.snapshot_weights, strict=True) if keys]
        cycle = sum(weight for _, weight in groups)
        batch, selected = [], set()
        while len(batch) < min(self.config.bridge_batch_size, len(admitted)):
            ticket = self.state.progress_cursor
            self.state.progress_cursor += 1
            turn, slot = divmod(ticket, cycle)
            for keys, weight in groups:
                if slot < weight:
                    key = keys[(turn * weight + slot) % len(keys)]
                    break
                slot -= weight
            if key not in selected:
                batch.append(key)
                selected.add(key)
        rows = self.adapter.population_read_earned_snapshot(batch)
        by_key = {row.identity_key: row for row in rows}
        if len(by_key) != len(rows) or set(by_key) != set(batch):
            raise BridgeError("earned snapshot returned duplicate, missing, or unrequested identities")
        for key in batch:
            record = by_key[key]
            binding = self.state.bindings.get(key)
            if (
                binding is None
                or record.character_guid != binding.character_guid
                or not 1 <= record.level <= 80
                or record.xp < 0
                or record.played_seconds < 0
                or record.zone_id < 0
                or record.map_id < 0
            ):
                raise BridgeError(f"earned snapshot is invalid for {key}")
            self.state.progress_cache[key] = record

    def _assignments(self, owner_level: int, phase_ceiling: int) -> tuple[BandAssignment, ...]:
        return classify_earned_progress(
            (
                self.state.progress_cache[key]
                for key in sorted(self.state.progress_cache)
                if key in self.state.admitted_identity_keys
            ),
            self.config.bands,
            owner_level,
            phase_ceiling,
            self.config.admission_capacity if self.config.rank_denominator == "admission_capacity" else None,
        )

    def _desired_sessions(
        self,
        assignments: tuple[BandAssignment, ...],
        session_limit: int,
        now: int,
        phase_ceiling: int,
        rotation_keys: set[str] | None = None,
    ) -> set[str]:
        rotation_keys = rotation_keys or set()
        by_band: dict[str, list[BandAssignment]] = {
            band.name: [] for band in self.config.bands
        }
        eligible_by_key: dict[str, BandAssignment] = {}
        pending_eligible: set[str] = set()
        for assignment in assignments:
            record = self.state.progress_cache[assignment.identity_key]
            if (record.level <= phase_ceiling
                    and (self.config.allow_overcap_sessions or record.level <= assignment.effective_ceiling)):
                if record.zone_id > 0:
                    by_band[assignment.band].append(assignment)
                    eligible_by_key[assignment.identity_key] = assignment
                elif record.zone_id == 0 and record.session_requested:
                    pending_eligible.add(assignment.identity_key)
        desired: set[str] = set()
        active = {
            key for key, record in self.state.progress_cache.items()
            if record.session_requested or record.online
        }
        rotation_keys &= active
        incumbents = active - rotation_keys
        all_items = [item for rows in by_band.values() for item in rows]
        # Keep a native request alive while its first earned snapshot still has
        # no zone; releasing that pending login causes one-second churn.
        desired.update(
            key for key in incumbents
            if key in eligible_by_key or key in pending_eligible
        )
        for key in desired:
            self.state.last_selected_epoch.setdefault(key, now)
        newcomers = {
            item.identity_key
            for item in all_items
            if item.identity_key not in active
            and self.state.activation_count.get(item.identity_key, 0) == 0
        }
        density_counts: dict[tuple[int, int], int] = {}

        def density_key(item: BandAssignment) -> tuple[int, int] | None:
            record = self.state.progress_cache[item.identity_key]
            if record.zone_id not in self.config.starter_zone_ids:
                return None
            # Keep the existing zone cap, but do not let a same-numbered zone on
            # another map consume the first map's budget without area telemetry.
            return record.map_id, record.zone_id

        for key in desired:
            assignment = eligible_by_key.get(key)
            if assignment is None:
                continue
            density = density_key(assignment)
            if density is not None:
                density_counts[density] = density_counts.get(density, 0) + 1

        def order(item: BandAssignment) -> tuple[int, int, int, int, str]:
            key = item.identity_key
            record = self.state.progress_cache[key]
            return (
                0 if key in newcomers else 1,
                self.state.activation_count.get(key, 0),
                record.played_seconds,
                self.state.last_selected_epoch.get(key, 0),
                key,
            )

        def add(item: BandAssignment) -> bool:
            key = item.identity_key
            if len(desired) >= session_limit or key in desired:
                return False
            density = density_key(item)
            if density is not None:
                count = density_counts.get(density, 0)
                if count >= self.config.starter_zone_online_cap:
                    return False
                density_counts[density] = count + 1
            desired.add(key)
            # A lease ages while it is held. Refreshing this on every tick made
            # the old roster permanently sticky and prevented newcomer turnover.
            self.state.last_selected_epoch.setdefault(key, now)
            return True

        def take(items: list[BandAssignment], limit: int) -> int:
            selected = 0
            for item in sorted(items, key=order):
                if add(item):
                    selected += 1
                if selected >= limit:
                    break
            return selected

        arrival_slots = max(0, session_limit - len(desired))
        newcomer_target = min(
            len(newcomers),
            int(
                (Decimal(arrival_slots) * self.config.newcomer_session_share).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        newcomer_quotas = allocate_exact(newcomer_target, self.config.bands)
        arrival_quotas = allocate_exact(arrival_slots, self.config.bands)
        for index, (band, quota) in enumerate(zip(self.config.bands, arrival_quotas, strict=True)):
            if quota == 0:
                continue
            rows = by_band[band.name]
            selected = take(
                [item for item in rows if item.identity_key in newcomers],
                min(quota, newcomer_quotas[index]),
            )
            remaining = quota - selected
            take(
                [item for item in rows
                 if item.identity_key not in active and item.identity_key not in newcomers],
                remaining,
            )

        # Incumbents are never evicted to satisfy an arrival quota. Explicit stale
        # rotation is the only path that can put an active identity in release.
        for item in sorted((item for item in all_items if item.identity_key not in active), key=order):
            if len(desired) >= session_limit:
                break
            add(item)
        return desired

    def _arrival_order(
        self,
        candidates: set[str],
        active: set[str],
        limit: int,
    ) -> list[str]:
        """Choose the next bounded arrivals from planned role/faction/region deficits."""
        role_units = {"tank": 1, "healer": 1, "damage": 3}
        role_counts: dict[str, int] = {}
        faction_counts: dict[str, int] = {}
        region_counts: dict[str, int] = {}
        for key in active:
            identity = self._identity_by_key.get(key)
            if identity is None:
                continue
            role_counts[identity.role_intent] = role_counts.get(identity.role_intent, 0) + 1
            faction_counts[identity.faction] = faction_counts.get(identity.faction, 0) + 1
            region_counts[identity.starter_region] = region_counts.get(identity.starter_region, 0) + 1

        remaining = set(candidates)
        ordered: list[str] = []
        # ponytail: scan at most five arrivals; replace with a heap only if the
        # configured per-window batch ever grows beyond this safety bound.
        def sort_key(candidate: str) -> tuple[Fraction, int, int, int, int, int, str]:
            identity = self._identity_by_key[candidate]
            record = self.state.progress_cache.get(candidate)
            return (
                Fraction(
                    role_counts.get(identity.role_intent, 0),
                    role_units.get(identity.role_intent, 3),
                ),
                faction_counts.get(identity.faction, 0),
                region_counts.get(identity.starter_region, 0),
                self.state.activation_count.get(candidate, 0),
                record.played_seconds if record is not None else 0,
                self.state.last_selected_epoch.get(candidate, 0),
                candidate,
            )

        for _ in range(min(limit, len(remaining))):
            key = min(remaining, key=sort_key)
            remaining.remove(key)
            ordered.append(key)
            identity = self._identity_by_key[key]
            role_counts[identity.role_intent] = role_counts.get(identity.role_intent, 0) + 1
            faction_counts[identity.faction] = faction_counts.get(identity.faction, 0) + 1
            region_counts[identity.starter_region] = region_counts.get(identity.starter_region, 0) + 1
        return ordered

    def _rotation_candidates(
        self,
        assignments: tuple[BandAssignment, ...],
        session_limit: int,
        now: int,
        phase_ceiling: int,
    ) -> set[str]:
        if session_limit <= 0:
            return set()
        current = {
            key for key, record in self.state.progress_cache.items() if record.session_requested
        }
        eligible = {
            item.identity_key
            for item in assignments
            if self.state.progress_cache[item.identity_key].zone_id > 0
            and self.state.progress_cache[item.identity_key].level <= phase_ceiling
            and (
                self.config.allow_overcap_sessions
                or self.state.progress_cache[item.identity_key].level <= item.effective_ceiling
            )
        }
        if not eligible - current:
            return set()
        stale = [
            key
            for key in current & eligible
            if now - self.state.last_selected_epoch.get(key, now)
            >= self.config.session_rotation_interval_seconds
        ]
        stale.sort(
            key=lambda key: (
                self.state.last_selected_epoch.get(key, now),
                -self.state.progress_cache[key].played_seconds,
                self.state.activation_count.get(key, 0),
                key,
            )
        )
        return set(stale[: min(self.config.session_rotation_max_per_tick, self.config.action_batch_size)])

    def _tick(self, now: int) -> bool:
        if not self._artifacts_unchanged():
            return self._fail_closed("immutable_release_artifact_changed", now)
        if self._recovery_pending:
            if not self.adapter.population_fail_closed():
                # Refusal is not proof of release: acknowledge the tick without scheduling.
                return True
            self._recovery_pending = False
            self.state.desired_session_keys.clear()
            # Revoke ACK is not a logout ACK; preserve observed sessions for reconciliation.

        phase = self.adapter.population_read_phase_snapshot()
        if not phase.enabled or not phase.policy_valid or not 1 <= phase.level_ceiling <= 80:
            return self._fail_closed("shared_progression_snapshot_invalid", now)
        owner = self.adapter.population_read_owner_high_water()
        self._fresh(owner.observed_epoch, now, "owner high-water")
        if not owner.authenticated or not 1 <= owner.level <= 80:
            return self._fail_closed("owner_high_water_invalid", now)
        capacity = self.adapter.population_read_capacity_snapshot()
        self._fresh(capacity.observed_epoch, now, "capacity")
        if (
            not capacity.valid
            or capacity.generation <= 0
            or capacity.online_limit < 0
            or capacity.available_sessions < 0
        ):
            return self._fail_closed("capacity_snapshot_invalid", now)

        # Rebuild native account classification before any admission/session work.
        # Replay existing identities independently of free online slots.
        for _ in range(min(self.config.action_batch_size, len(self._recovery_keys))):
            key = self._recovery_keys[0]
            try:
                self._bind_and_admit(self._identity_by_key[key])
            except PendingOperation:
                save_state(self.config.state_path, self.state)
                return True
            self._recovery_keys.popleft()
        if self._recovery_keys:
            save_state(self.config.state_path, self.state)
            return True

        self._refresh_progress()
        requested_now = sum(record.session_requested for record in self.state.progress_cache.values())
        # Native ordinary creation persists an offline character. Session fullness
        # is not roster pressure; validated capacity + cadence bound this work.
        admission_slots = self.config.admission_batch_size if capacity.online_limit > 0 else 0
        admitted = self._admit_due_batch(owner.level, now, admission_slots)
        # New admissions join session selection after their next earned-state read.
        assignments = self._assignments(owner.level, phase.level_ceiling)
        session_limit = min(
            self.config.online_cap,
            capacity.online_limit,
            requested_now + capacity.available_sessions,
        )
        rotation_keys = self._rotation_candidates(
            assignments, session_limit, now, phase.level_ceiling
        )
        desired = self._desired_sessions(
            assignments, session_limit, now, phase.level_ceiling, rotation_keys
        )
        current = {
            key for key, record in self.state.progress_cache.items() if record.session_requested
        }
        # Offline unselected identities need no permission. Refresh active/protected and
        # newly selected identities before any session changes; native defaults stay held.
        policy_keys = current | desired | {
            key for key, record in self.state.progress_cache.items() if record.online
        }
        policies = [item for item in assignments if item.identity_key in policy_keys]
        for start in range(0, len(policies), 256):
            rows = [
                {
                    "identity_key": item.identity_key,
                    "effective_ceiling": item.effective_ceiling,
                    "held": (self.state.progress_cache[item.identity_key].zone_id == 0
                             or self.state.progress_cache[item.identity_key].level >= item.effective_ceiling),
                }
                for item in policies[start : start + 256]
            ]
            if not self.adapter.population_apply_progression_policy(rows):
                raise BridgeError("native progression policy was rejected")
        self._published_progression_eligible = {
            item.identity_key for item in policies
            if (self.state.progress_cache[item.identity_key].zone_id > 0
                and self.state.progress_cache[item.identity_key].level < item.effective_ceiling)
        }
        online = {key for key, record in self.state.progress_cache.items() if record.online}
        release = sorted((current | online) - desired)
        # Resume after the last attempt so protected sessions cannot starve later keys.
        start = bisect_right(release, self._last_release_key)
        release = release[start:] + release[:start]
        request_candidates = desired - current - online
        actions: list[dict[str, object]] = []
        remaining = self.config.action_batch_size
        release_deferred = False
        for key in release:
            if remaining == 0:
                break
            remaining -= 1
            self._last_release_key = key
            if not self.adapter.population_release_session(key):
                # False is an unacknowledged teardown, not permission to clear
                # its occupied slot or replace a protected/loading session.
                release_deferred = True
                actions.append({"identity_key": key, "requested": True,
                                "result": "release_not_acknowledged"})
                continue
            self.state.progress_cache[key] = replace(
                self.state.progress_cache[key], session_requested=False, online=False
            )
            actions.append({"identity_key": key, "requested": False})
        arrival_cadence_ready = (
            not self.state.last_admission_epoch
            or now - self.state.last_admission_epoch >= self.config.admission_interval_seconds
        )
        request_budget = min(
            self.config.admission_batch_size,
            max(0, capacity.available_sessions),
        ) if arrival_cadence_ready and not release_deferred else 0
        request = self._arrival_order(request_candidates, current | online, request_budget)
        requested = 0
        arrival_keys: list[str] = []
        if release_deferred:
            request = []
        for key in request:
            if remaining == 0 or requested >= request_budget:
                break
            if not self.adapter.population_request_session(key, capacity.generation):
                raise BridgeError(f"session request was rejected for {key}")
            self.state.progress_cache[key] = replace(
                self.state.progress_cache[key], session_requested=True
            )
            self.state.activation_count[key] = self.state.activation_count.get(key, 0) + 1
            actions.append({"identity_key": key, "requested": True})
            arrival_keys.append(key)
            remaining -= 1
            requested += 1
        if requested:
            # Reuse the existing durable arrival timestamp so reloads cannot burst
            # more than one configured batch before the 30-second cadence expires.
            self.state.last_admission_epoch = now
        self.state.desired_session_keys = desired
        self.state.revision += 1
        save_state(self.config.state_path, self.state)
        append_audit(
            self.config.audit_path,
            {
                "actions": actions,
                "admitted_identity_keys": admitted,
                "capability_generation": self.capabilities.generation,
                "capacity_generation": capacity.generation,
                "epoch": now,
                "manifest_digest": self.manifest.manifest_digest,
                "eligibility_digest": self.manifest.eligibility_digest,
                "eligibility_generation": self.manifest.eligibility_generation,
                "owner_level": owner.level,
                "phase_id": phase.phase_id,
                "phase_policy_digest": phase.policy_digest,
                "revision": self.state.revision,
                "result": "ok",
                "session_target": len(desired),
                "rotated_identity_keys": sorted(set(release) & rotation_keys),
                "session_request_budget": request_budget,
                "session_request_cadence_ready": arrival_cadence_ready,
                "session_requests": requested,
                "arrival_role_evidence": "manifest_role_intent_only",
                "arrival_functional_role_ready": False,
                "arrival_roles": {
                    role: sum(
                        self._identity_by_key[key].role_intent == role for key in arrival_keys
                    )
                    for role in ("tank", "healer", "damage")
                },
                "arrival_factions": {
                    faction: sum(
                        self._identity_by_key[key].faction == faction for key in arrival_keys
                    )
                    for faction in ("alliance", "horde")
                },
                "arrival_regions": {
                    region: sum(
                        self._identity_by_key[key].starter_region == region for key in arrival_keys
                    )
                    for region in sorted(
                        {self._identity_by_key[key].starter_region for key in arrival_keys}
                    )
                },
                "starter_density_scope": "map_zone",
            },
        )
        return True

    def tick(self, _diff_ms: int = 0) -> bool:
        now = int(self.clock())
        if now < self._last_tick_epoch:
            return self._fail_closed("clock_moved_backwards", now)
        self._last_tick_epoch = now
        try:
            return self._tick(now)
        except Exception as exc:
            try:
                self._fail_closed(type(exc).__name__, now)
            except Exception as shutdown:
                raise BridgeError(
                    f"{type(exc).__name__}: {exc}; fail-close also failed: "
                    f"{type(shutdown).__name__}: {shutdown}"
                ) from exc
            raise
