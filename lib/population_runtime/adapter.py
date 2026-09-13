"""Typed semantic contract for the deliberately narrow native bridge."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .capabilities import InstalledCapabilities, require_population_planning_capabilities
from .errors import BridgeError, PendingOperation
from .models import (
    CapacitySnapshot,
    EarnedProgress,
    IdentityBinding,
    OwnerHighWater,
    PhaseSnapshot,
)


class PopulationPlanningAdapter(Protocol):
    """Observation-only ports available to the pre-binding policy planner."""

    def read_owner_high_water(self) -> OwnerHighWater: ...

    def read_earned_snapshot(self, identity_keys: Sequence[str]) -> list[EarnedProgress]: ...

    def read_capacity_snapshot(self) -> CapacitySnapshot: ...

    def read_phase_snapshot(self) -> PhaseSnapshot: ...


class PopulationAdapter(Protocol):
    """Synchronous, idempotent methods callable only during the fenced world-thread tick."""

    def population_read_owner_high_water(self) -> OwnerHighWater: ...

    def population_read_earned_snapshot(
        self, identity_keys: Sequence[str]
    ) -> list[EarnedProgress]: ...

    def population_reserve_identity(
        self, identity_key: str, account_key: str, guid_allocation_key: str, wave_key: str
    ) -> bool: ...

    def population_ensure_account(
        self, account_key: str, account_name: str, expected_manifest_slots: int
    ) -> int: ...

    def population_ensure_character(
        self, identity_key: str, account_id: int, immutable_creation_fields: Mapping[str, Any]
    ) -> int: ...

    def population_bind_identity_once(
        self, identity_key: str, account_id: int, character_guid: int, manifest_digest: str
    ) -> IdentityBinding: ...

    def population_set_admission(
        self, identity_key: str, wave_key: str, manifest_digest: str
    ) -> bool: ...

    def population_apply_progression_policy(
        self, rows: Sequence[Mapping[str, object]]
    ) -> bool: ...

    def population_request_session(self, identity_key: str, capacity_generation: int) -> bool: ...

    def population_release_session(self, identity_key: str) -> bool: ...

    def population_read_capacity_snapshot(self) -> CapacitySnapshot: ...

    def population_read_phase_snapshot(self) -> PhaseSnapshot: ...

    def population_fail_closed(self) -> bool: ...


class AzerothCoreAdapter:
    """Adapter for the `azerothcore` population-v1 exports; it retains no native object."""

    def __init__(self, module: object) -> None:
        self._module = module

    def _call(self, name: str, *args: object) -> Any:
        return getattr(self._module, name)(*args)

    def population_read_owner_high_water(self) -> OwnerHighWater:
        raw = self._call("population_read_owner_high_water")
        return OwnerHighWater(
            character_guid=int(raw["character_guid"]),
            level=int(raw["level"]),
            authenticated=bool(raw["authenticated"]),
            observed_epoch=int(raw["observed_epoch"]),
        )

    def population_read_earned_snapshot(
        self, identity_keys: Sequence[str]
    ) -> list[EarnedProgress]:
        rows = self._call("population_read_earned_snapshot", list(identity_keys))
        return [
            EarnedProgress(
                identity_key=str(row["identity_key"]),
                character_guid=int(row["character_guid"]),
                level=int(row["level"]),
                xp=int(row["xp"]),
                played_seconds=int(row["played_seconds"]),
                map_id=int(row["map_id"]),
                zone_id=int(row["zone_id"]),
                session_requested=bool(row["session_requested"]),
                online=bool(row["online"]),
            )
            for row in rows
        ]

    def population_reserve_identity(
        self, identity_key: str, account_key: str, guid_allocation_key: str, wave_key: str
    ) -> bool:
        return bool(
            self._call(
                "population_reserve_identity",
                identity_key,
                account_key,
                guid_allocation_key,
                wave_key,
            )
        )

    def population_ensure_account(
        self, account_key: str, account_name: str, expected_manifest_slots: int
    ) -> int:
        return _creation_id(self._call(
            "population_ensure_account", account_key, account_name, expected_manifest_slots
        ), "account")

    def population_ensure_character(
        self, identity_key: str, account_id: int, immutable_creation_fields: Mapping[str, Any]
    ) -> int:
        return _creation_id(self._call(
            "population_ensure_character", identity_key, account_id, dict(immutable_creation_fields)
        ), "character")

    def population_bind_identity_once(
        self, identity_key: str, account_id: int, character_guid: int, manifest_digest: str
    ) -> IdentityBinding:
        raw = self._call(
            "population_bind_identity_once",
            identity_key,
            account_id,
            character_guid,
            manifest_digest,
        )
        return IdentityBinding(
            identity_key=str(raw["identity_key"]),
            account_id=int(raw["account_id"]),
            character_guid=int(raw["character_guid"]),
        )

    def population_set_admission(
        self, identity_key: str, wave_key: str, manifest_digest: str
    ) -> bool:
        return bool(
            self._call(
                "population_set_admission", identity_key, wave_key, manifest_digest
            )
        )

    def population_apply_progression_policy(
        self, rows: Sequence[Mapping[str, object]]
    ) -> bool:
        result = self._call("population_apply_progression_policy", [dict(row) for row in rows])
        if type(result) is not bool:
            raise BridgeError("native progression policy acknowledgement must be boolean")
        return result

    def population_request_session(self, identity_key: str, capacity_generation: int) -> bool:
        return bool(
            self._call("population_request_session", identity_key, capacity_generation)
        )

    def population_release_session(self, identity_key: str) -> bool:
        return bool(self._call("population_release_session", identity_key))

    def population_read_capacity_snapshot(self) -> CapacitySnapshot:
        raw = self._call("population_read_capacity_snapshot")
        return CapacitySnapshot(
            generation=int(raw["generation"]),
            online_limit=int(raw["online_limit"]),
            available_sessions=int(raw["available_sessions"]),
            valid=bool(raw["valid"]),
            observed_epoch=int(raw["observed_epoch"]),
        )

    def population_read_phase_snapshot(self) -> PhaseSnapshot:
        raw = self._call("population_read_phase_snapshot")
        return PhaseSnapshot(
            enabled=bool(raw["enabled"]),
            policy_valid=bool(raw["policy_valid"]),
            phase_id=str(raw["phase_id"]),
            level_ceiling=int(raw["level_ceiling"]),
            policy_digest=str(raw["policy_digest"]),
        )

    def population_fail_closed(self) -> bool:
        return bool(self._call("population_fail_closed"))


_OWNER_KEYS = {"character_guid", "level", "authenticated", "observed_epoch"}
_EARNED_KEYS = {
    "identity_key",
    "character_guid",
    "level",
    "xp",
    "played_seconds",
    "map_id",
    "zone_id",
    "session_requested",
    "online",
}
_CAPACITY_KEYS = {
    "generation",
    "online_limit",
    "available_sessions",
    "valid",
    "observed_epoch",
}
_PHASE_KEYS = {"enabled", "policy_valid", "phase_id", "level_ceiling", "policy_digest"}


def _mapping(raw: object, name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise BridgeError(f"{name} must be a mapping with string keys")
    return raw


def _exact_mapping(raw: object, keys: set[str], name: str) -> Mapping[str, object]:
    value = _mapping(raw, name)
    if set(value) != keys:
        raise BridgeError(f"{name} has an unexpected key set")
    return value


def _integer(raw: Mapping[str, object], key: str, name: str, minimum: int = 0) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BridgeError(f"{name}.{key} must be an integer >= {minimum}")
    return value


def _boolean(raw: Mapping[str, object], key: str, name: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise BridgeError(f"{name}.{key} must be a boolean")
    return value


def _string(raw: Mapping[str, object], key: str, name: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise BridgeError(f"{name}.{key} must be a non-empty string")
    return value


def _creation_id(raw: object, name: str) -> int:
    value = _exact_mapping(raw, {"id", "pending"}, name)
    identity_id = _integer(value, "id", name)
    pending = _boolean(value, "pending", name)
    if pending and identity_id == 0:
        raise PendingOperation(f"native {name} creation is pending")
    if pending or identity_id == 0:
        raise BridgeError(f"native {name} creation was rejected or invalid")
    return identity_id


class AzerothCoreReadOnlyAdapter:
    """Expose only native observations to a policy-only planner.

    The complete :class:`AzerothCoreAdapter` above is retained for the later
    lifecycle gate.  This adapter intentionally has no account, character,
    identity-binding, admission, or session mutation methods.  Its private
    module reference is never handed to policy code.
    """

    def __init__(self, module: object, capabilities: InstalledCapabilities) -> None:
        require_population_planning_capabilities(capabilities)
        self._module = module

    def _call(self, name: str, *args: object) -> object:
        try:
            function = getattr(self._module, name)
        except AttributeError as exc:
            raise BridgeError(f"required planning port {name} is unavailable") from exc
        if not callable(function):
            raise BridgeError(f"required planning port {name} is not callable")
        return function(*args)

    def read_owner_high_water(self) -> OwnerHighWater:
        raw = _exact_mapping(self._call("population_read_owner_high_water"), _OWNER_KEYS, "owner")
        return OwnerHighWater(
            character_guid=_integer(raw, "character_guid", "owner"),
            level=_integer(raw, "level", "owner"),
            authenticated=_boolean(raw, "authenticated", "owner"),
            observed_epoch=_integer(raw, "observed_epoch", "owner"),
        )

    def read_earned_snapshot(self, identity_keys: Sequence[str]) -> list[EarnedProgress]:
        keys = list(identity_keys)
        if len(keys) > 256 or any(not isinstance(key, str) or not key for key in keys):
            raise BridgeError("planning identity snapshot keys must contain 0..256 non-empty strings")
        if len(keys) != len(set(keys)):
            raise BridgeError("planning identity snapshot keys must be unique")
        raw_rows = self._call("population_read_earned_snapshot", keys)
        if not isinstance(raw_rows, (list, tuple)) or len(raw_rows) > 256:
            raise BridgeError("earned snapshot must be a bounded list")
        rows: list[EarnedProgress] = []
        for raw in raw_rows:
            value = _exact_mapping(raw, _EARNED_KEYS, "earned row")
            rows.append(
                EarnedProgress(
                    identity_key=_string(value, "identity_key", "earned row"),
                    character_guid=_integer(value, "character_guid", "earned row"),
                    level=_integer(value, "level", "earned row"),
                    xp=_integer(value, "xp", "earned row"),
                    played_seconds=_integer(value, "played_seconds", "earned row"),
                    map_id=_integer(value, "map_id", "earned row"),
                    zone_id=_integer(value, "zone_id", "earned row"),
                    session_requested=_boolean(value, "session_requested", "earned row"),
                    online=_boolean(value, "online", "earned row"),
                )
            )
        return rows

    def read_capacity_snapshot(self) -> CapacitySnapshot:
        raw = _exact_mapping(
            self._call("population_read_capacity_snapshot"), _CAPACITY_KEYS, "capacity"
        )
        return CapacitySnapshot(
            generation=_integer(raw, "generation", "capacity"),
            online_limit=_integer(raw, "online_limit", "capacity"),
            available_sessions=_integer(raw, "available_sessions", "capacity"),
            valid=_boolean(raw, "valid", "capacity"),
            observed_epoch=_integer(raw, "observed_epoch", "capacity"),
        )

    def read_phase_snapshot(self) -> PhaseSnapshot:
        raw = _exact_mapping(self._call("population_read_phase_snapshot"), _PHASE_KEYS, "phase")
        return PhaseSnapshot(
            enabled=_boolean(raw, "enabled", "phase"),
            policy_valid=_boolean(raw, "policy_valid", "phase"),
            phase_id=_string(raw, "phase_id", "phase"),
            level_ceiling=_integer(raw, "level_ceiling", "phase"),
            policy_digest=_string(raw, "policy_digest", "phase"),
        )
