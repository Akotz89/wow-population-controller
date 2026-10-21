"""Bounded CPI-207 accepted-quest policy; native Playerbots executes every action.

Compose after a successfully published population tick. This is not the full
route/gearing planner. Native quest history is authoritative across reloads;
Optional durable outcome memory is character-scoped, not full route failure telemetry.
"""
from __future__ import annotations

import time
import hashlib
import json
import os
import tempfile
from pathlib import Path
from collections.abc import Callable, Mapping


class ProtocolError(ValueError):
    pass


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"invalid {name}")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


class QuestMemory:
    """Small transition-only store; same atomic JSON pattern as population state."""
    schema = "cpi207-quest-outcomes-v1"
    reasons = {"quest_failed", "native_failed_quest", "goal_lost"}

    def __init__(self, path):
        self.path = Path(path)
        self.goals, self.failures = {}, {}
        if self.path.is_symlink() or not self.path.parent.is_dir() or self.path.parent.is_symlink():
            raise ProtocolError("invalid quest memory path")
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            claimed = raw.pop("digest", None)
            if set(raw) != {"schema", "goals", "failures"} or raw["schema"] != self.schema or _digest(raw) != claimed:
                raise ProtocolError("invalid quest memory document")
            if not isinstance(raw["goals"], dict) or not isinstance(raw["failures"], dict):
                raise ProtocolError("invalid quest memory records")
            for key, context in raw["goals"].items():
                self.validate_context(context)
                if key != context["identity_key"]:
                    raise ProtocolError("quest memory goal identity mismatch")
            for key, record in raw["failures"].items():
                if not isinstance(record, dict) or set(record) != {"context", "reason", "count", "last_epoch", "retry_epoch"}:
                    raise ProtocolError("invalid quest failure record")
                self.validate_context(record["context"])
                if key != _digest(record["context"]) or record["reason"] not in self.reasons:
                    raise ProtocolError("invalid quest failure context")
                for name in ("count", "last_epoch", "retry_epoch"):
                    value = _integer(record[name], name, 1)
                    if value > 2**63 - 1:
                        raise ProtocolError("quest failure value overflow")
                if record["retry_epoch"] - record["last_epoch"] != 30:
                    raise ProtocolError("invalid quest failure retry interval")
            self.goals, self.failures = raw["goals"], raw["failures"]
        self._saved = _canonical(self.payload())

    @staticmethod
    def validate_context(context):
        fields = {"source_sha256", "policy_version", "phase_id", "phase_digest", "identity_key", "account_id", "character_guid", "quest_id", "level"}
        if not isinstance(context, dict) or set(context) != fields:
            raise ProtocolError("invalid quest context")
        for name in ("source_sha256", "phase_digest"):
            value = context[name]
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ProtocolError("invalid quest context digest")
        for name in ("policy_version", "phase_id", "identity_key"):
            if not isinstance(context[name], str) or not context[name]:
                raise ProtocolError("invalid quest context identity")
        for name in ("account_id", "character_guid", "quest_id", "level"):
            _integer(context[name], name, 1)

    def payload(self):
        return dict(schema=self.schema, goals=self.goals, failures=self.failures)

    def save(self):
        payload = self.payload()
        encoded = _canonical(payload)
        if encoded == self._saved:
            return
        fd, temporary = tempfile.mkstemp(prefix="." + self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical(dict(payload, digest=_digest(payload))) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self._saved = encoded
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class QuestPolicy:
    policy_version = "cpi207-accepted-quest-v1"

    def __init__(self, native, audit: Callable[[dict], None], clock=time.monotonic,
                 max_per_tick: int = 8, retry_seconds: int = 30, *,
                 memory_path=None, source_sha256=None, wall_clock=lambda: int(time.time()),
                 renewal_budget: int = 1024, tick_interval_seconds: int = 1, acquisition_capture=None):
        self.native, self.audit, self.clock = native, audit, clock
        self.acquisition_capture = acquisition_capture
        self.limit = _integer(max_per_tick, "max_per_tick", 1)
        if self.limit > 256:
            raise ProtocolError("max_per_tick exceeds 256")
        self.renewal_budget = _integer(renewal_budget, "renewal_budget", 1)
        self.tick_interval_seconds = _integer(tick_interval_seconds, "tick_interval_seconds", 1)
        if self.renewal_budget > 10000 or self.tick_interval_seconds not in (1, 2):
            raise ProtocolError("quest cadence must be1 or2 seconds and renewal budget at most10000")
        # Native lease is5s. Reserve2s for callback jitter; late renewals fail closed natively.
        self.active_capacity = self.renewal_budget * (3 // self.tick_interval_seconds)
        self.active: dict[str, int] = {}
        self.renewal_cursor = 0
        self.next_tick = None
        self.retry_seconds = _integer(retry_seconds, "retry_seconds", 1)
        self.goals: dict[str, int] = {}
        self.retry: dict[tuple[str, int], float] = {}
        self.cursor = 0
        self._renewal_audits: dict[str, tuple] = {}
        self.last_time = None
        self.memory = QuestMemory(memory_path) if memory_path is not None else None
        self.source_sha256 = source_sha256
        self.wall_clock = wall_clock
        self._wall = None
        self._phase = None
        if self.memory is not None:
            if retry_seconds != 30 or not isinstance(source_sha256, str) or len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
                raise ProtocolError("durable memory requires exact source digest and 30-second retry")
            self.goals = {key: value["quest_id"] for key, value in self.memory.goals.items()}
            self._wall = max((r["last_epoch"] for r in self.memory.failures.values()), default=0)

    def _context(self, key, binding, quest, snapshot):
        context = dict(source_sha256=self.source_sha256, policy_version=self.policy_version,
                       phase_id=self._phase["phase_id"], phase_digest=self._phase["policy_digest"].lower(),
                       identity_key=key, account_id=binding[0], character_guid=binding[1],
                       quest_id=quest, level=snapshot["level"])
        QuestMemory.validate_context(context)
        return context

    def _capture_event(self, event, *args):
        observer = self.acquisition_capture
        if observer is not None:
            try:
                getattr(observer, event)(*args, self.source_sha256)
            except Exception as error:
                # Observation failures cannot change the ordinary native goal result.
                observer.fail(type(error).__name__ + ': ' + str(error))

    def _forget_goal(self, key):
        self._capture_event('lost', key, self.goals.get(key, 0))
        self.active.pop(key, None)
        self.goals.pop(key, None)
        if self.memory is not None:
            self.memory.goals.pop(key, None)
            self.memory.save()

    def _emit(self, key, event, quest_id, reason="", *, observation=None):
        # Lease renewal still runs every tick. Only redundant audit persistence
        # is skipped; any intervening event invalidates this in-memory cache.
        signature = (quest_id, reason, observation)
        if event == "renewed" and self._renewal_audits.get(key) == signature:
            return
        if event != "renewed":
            self._renewal_audits.pop(key, None)
        self.audit(dict(policy_version=self.policy_version, identity_key=key,
                        event=event, quest_id=quest_id, reason=reason))
        if event == "renewed":
            self._renewal_audits[key] = signature

    def _snapshot(self, key, binding, observed):
        s = self.native.strategic_read_quest_snapshot(key, observed)
        if not isinstance(s, dict) or s.get("identity_key") != key:
            raise ProtocolError("snapshot identity mismatch")
        account = _integer(s.get("account_id"), "account_id", 1)
        guid = _integer(s.get("character_guid"), "character_guid", 1)
        if (account, guid) != tuple(binding):
            raise ProtocolError("snapshot durable binding mismatch")
        for name in ("online", "control_allowed"):
            if type(s.get(name)) is not bool:
                raise ProtocolError(f"invalid {name}")
        if not isinstance(s.get("reason"), str):
            raise ProtocolError("missing control reason")
        _integer(s.get("current_goal_quest_id"), "current_goal_quest_id")
        if type(s.get("current_goal_acquiring", False)) is not bool:
            raise ProtocolError("invalid acquisition stage")
        if s.get("observed_quest_id") != observed:
            raise ProtocolError("observed quest mismatch")
        if s.get("observed_quest_status") not in {"none", "incomplete", "complete", "rewarded", "failed"}:
            raise ProtocolError("invalid observed quest status")
        quests = s.get("quests")
        if not isinstance(quests, list) or len(quests) > 25:
            raise ProtocolError("quest log exceeds supported bound")
        seen = set()
        for q in quests:
            if not isinstance(q, dict):
                raise ProtocolError("invalid quest row")
            qid = _integer(q.get("quest_id"), "quest_id", 1)
            if qid in seen or q.get("status") not in {"incomplete", "complete", "failed"}:
                raise ProtocolError("invalid or duplicate quest")
            seen.add(qid)
            if type(q.get("eligible")) is not bool or not isinstance(q.get("rejection_reason"), str):
                raise ProtocolError("invalid quest eligibility")
        return s

    def _one(self, key, binding, now):
        previous = self.goals.get(key, 0)
        s = self._snapshot(key, binding, previous)
        status = s["observed_quest_status"]
        if previous and status == "rewarded":
            self._capture_event('rewarded', key, binding, previous)
            self._forget_goal(key)
            self._emit(key, "rewarded", previous, "native_rewarded_history")
            previous = 0
        if not s["online"] or not s["control_allowed"]:
            if previous and s["current_goal_quest_id"] != previous:
                self._forget_goal(key)  # A goal lost during explicit suspension is not a failure.
            self._emit(key, "deferred", previous, s["reason"])
            return
        if previous and self.memory is not None and self.memory.goals[key] != self._context(key, binding, previous, s):
            self._forget_goal(key)  # A changed implementation/phase/level is not an executor failure.
            previous = 0
        current = s["current_goal_quest_id"]
        if previous and (status == "failed" or current != previous or
                         (status == "none" and not self._pending_goal(previous, s))):
            if self.memory is None:
                self.retry[key, previous] = now + self.retry_seconds
            reason = "quest_failed" if status == "failed" else "goal_lost"
            if any(q["quest_id"] == previous and q["rejection_reason"] == "native_failed_quest" for q in s["quests"]):
                reason = "native_failed_quest"
            if self.memory is not None:
                context = self._context(key, binding, previous, s)
                digest = _digest(context)
                old = self.memory.failures.get(digest)
                if old and old["count"] >= 2**63 - 1:
                    raise ProtocolError("quest failure count overflow")
                self.memory.failures[digest] = dict(context=context, reason=reason,
                    count=old["count"] + 1 if old else 1, last_epoch=self._wall, retry_epoch=self._wall + 30)
                self.memory.goals.pop(key, None)
                self.memory.save()
            self._capture_event('lost', key, previous)
            self._emit(key, "lost", previous, reason)
            self.goals.pop(key, None)
        eligible = [q for q in s["quests"] if q["eligible"] and q["status"] != "failed"
                    and now >= self.retry.get((key, q["quest_id"]), 0)]
        if self.memory is not None:
            eligible = [q for q in eligible if self._wall >= self.memory.failures.get(
                _digest(self._context(key, binding, q["quest_id"], s)), {}).get("retry_epoch", 0)]
        eligible.sort(key=lambda q: (q["quest_id"] != current,
                                     q["status"] != "complete", q["quest_id"]))
        quest = self._select_goal(key, binding, s, eligible, now)
        if quest is None:
            return
        if key not in self.active and len(self.active) >= self.active_capacity:
            self._emit(key, "deferred", quest, "renewal_capacity")
            return
        result = self._set_goal(key, quest)
        if (not isinstance(result, dict) or result.get("quest_id") != quest
                or result.get("status") not in {"accepted", "deferred", "rejected"}
                or not isinstance(result.get("reason"), str)):
            raise ProtocolError("invalid native goal acknowledgement")
        outcome = result["status"]
        if outcome == "accepted":
            self._capture_event('accepted', key, binding, quest)
            self.goals[key] = quest
            self.active[key] = quest
            if self.memory is not None:
                self.memory.goals[key] = self._context(key, binding, quest, s)
                self.memory.save()
            # Native v10 exposes quest status/eligibility, not objective counters.
            # Compare its bounded (<=25) observations without per-tick hashing.
            phase = self._phase or {}
            observation = (tuple(binding), s.get("level"), s["observed_quest_status"],
                           phase.get("phase_id"), phase.get("policy_digest"),
                           tuple(sorted((q["quest_id"], q["status"], q["eligible"],
                                         q["rejection_reason"]) for q in s["quests"])))
            self._emit(key, "renewed" if current == quest else "selected", quest,
                       result["reason"], observation=observation)
        else:
            self.retry[key, quest] = now + self.retry_seconds
            self._emit(key, outcome, quest, result["reason"])

    def _pending_goal(self, quest, snapshot):
        return False

    def _set_goal(self, key, quest):
        return self.native.strategic_set_quest_goal(key, quest)

    def _select_goal(self, key, binding, snapshot, eligible, now):
        """Override selection only; leases, failure memory and setter stay shared."""
        if not eligible:
            self._emit(key, "deferred", 0, "no_supported_eligible_quest")
            return None
        return eligible[0]["quest_id"]

    def inherit_schedule(self, previous):
        # Read-only copy: unsuccessful reload never mutates the active interpreter.
        self.cursor = previous.cursor
        self.renewal_cursor = previous.renewal_cursor
        self.next_tick = previous.next_tick
        self.last_time = previous.last_time
        self.retry = dict(previous.retry)
        self.active = dict(list(sorted(previous.active.items()))[:self.active_capacity])

    def _renew(self, bindings):
        # Removal or expected hold stops renewal; native expires untouched leases within5s.
        for key, quest in self.active.items():
            if key not in bindings:
                self._capture_event('lost', key, quest)
        self.active = {key: quest for key, quest in self.active.items() if key in bindings}
        keys = sorted(self.active)
        if not keys:
            return
        start = self.renewal_cursor % len(keys)
        selected = [keys[(start + n) % len(keys)] for n in range(min(self.renewal_budget, len(keys)))]
        self.renewal_cursor = (start + len(selected)) % len(keys)
        for offset in range(0, len(selected), 256):
            batch = selected[offset:offset + 256]
            rows = [{"identity_key": key, "quest_id": self.active[key]} for key in batch]
            result = self.native.strategic_renew_quest_goals(rows)
            if not isinstance(result, list) or len(result) != len(rows):
                raise ProtocolError("quest renewal result count mismatch")
            for key, row, reply in zip(batch, rows, result, strict=True):
                if (not isinstance(reply, dict) or reply.get("quest_id") != row["quest_id"]
                        or reply.get("status") not in {"accepted", "deferred"}
                        or not isinstance(reply.get("reason"), str)):
                    raise ProtocolError("invalid quest renewal acknowledgement")
                if reply["status"] != "accepted":
                    self._capture_event('lost', key, row["quest_id"])
                    self.active.pop(key, None)
                    self._emit(key, "deferred", row["quest_id"], reply["reason"])

    def tick(self, bindings: Mapping[str, tuple[int, int]]) -> None:
        """Visit at most limit identities; callers supply admitted online candidates.

        No account creation, session requests, quest acceptance or reward API is
        reachable here. Protocol errors propagate so the enclosing fenced tick
        can use its existing fail-closed path.
        """
        now = self.clock()
        if self.last_time is not None and now < self.last_time:
            raise ProtocolError("clock moved backwards")
        self.last_time = now
        if self.next_tick is not None and now < self.next_tick:
            return
        self.next_tick = now + self.tick_interval_seconds
        self._renew(bindings)
        keys = sorted(bindings)
        if not keys:
            return
        if self.memory is not None:
            wall = _integer(self.wall_clock(), "wall epoch", 1)
            if wall > 2**63 - 31 or wall < (self._wall or 0):
                raise ProtocolError("wall clock moved backwards or overflowed")
            self._wall = wall
            # Existing provider reads its shared in-memory phase snapshot, not the DB.
            self._phase = self.native.population_read_phase_snapshot()
            if not isinstance(self._phase, dict) or self._phase.get("enabled") is not True or self._phase.get("policy_valid") is not True:
                raise ProtocolError("durable quest memory requires valid native phase")
        start = self.cursor % len(keys)
        count = min(self.limit, len(keys))
        for offset in range(count):
            key = keys[(start + offset) % len(keys)]
            self._one(key, bindings[key], now)
        self.cursor = (start + count) % len(keys)


def population_then_strategy(population, strategy: QuestPolicy, diff_ms: int) -> bool:
    """Existing bootstrap composition, not a population controller responsibility."""
    revision = population.state.revision
    result = population.tick(diff_ms)
    if result is True and population.state.revision > revision:
        state = population.state
        bindings = {
            key: (state.bindings[key].account_id, state.bindings[key].character_guid)
            for key, progress in state.progress_cache.items()
            if key in state.admitted_identity_keys and progress.online
            and key in population._published_progression_eligible
        }
        strategy.tick(bindings)
    return result


class CadencedPopulationStrategy:
    """Run the existing composition at most once per monotonic second."""

    def __init__(self, population, strategy, clock=time.monotonic):
        self.population = population
        self.strategy = strategy
        self.clock = clock
        self._next_tick = None
        self._failed = False

    def tick(self, diff_ms: int) -> bool:
        if self._failed:
            return False
        now = self.clock()
        if self._next_tick is not None and now < self._next_tick:
            return True
        self._next_tick = now + 1.0
        # A failed/raising callback must never become an acknowledged skipped tick.
        self._failed = True
        result = population_then_strategy(self.population, self.strategy, diff_ms)
        self._failed = result is not True
        return result
