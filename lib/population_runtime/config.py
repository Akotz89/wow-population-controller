"""Strict runtime configuration with bounded values and immutable artifact pins."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .canonical import load_json_object, digest_file
from .errors import ArtifactError
from .models import BandDefinition

SCHEMA = "cpi-223-python-population-runtime-v1"
_BAND_NAMES = ("pioneer", "scout", "forward", "main", "trailing")
_EXPECTED_KEYS = {
    "schema_version",
    "enabled",
    "manifest_path",
    "manifest_file_sha256",
    "capabilities_path",
    "capabilities_file_sha256",
    "identity_gate_path",
    "identity_gate_sha256",
    "decisions_path",
    "schedule_path",
    "template_catalogue_path",
    "appearance_catalogue_path",
    "state_path",
    "audit_path",
    "durable_target",
    "online_cap",
    "admission_batch_size",
    "admission_interval_seconds",
    "bridge_batch_size",
    "action_batch_size",
    "snapshot_max_age_seconds",
    "starter_zone_ids",
    "starter_zone_online_cap",
    "bands",
}


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    manifest_path: Path
    manifest_file_sha256: str
    capabilities_path: Path
    capabilities_file_sha256: str
    identity_gate_path: Path
    identity_gate_sha256: str
    decisions_path: Path
    schedule_path: Path
    template_catalogue_path: Path
    appearance_catalogue_path: Path
    state_path: Path
    audit_path: Path
    durable_target: int
    online_cap: int
    admission_batch_size: int
    admission_interval_seconds: int
    bridge_batch_size: int
    action_batch_size: int
    snapshot_max_age_seconds: int
    starter_zone_ids: frozenset[int]
    starter_zone_online_cap: int
    bands: tuple[BandDefinition, ...]
    pending_admission_limit: int = 1
    snapshot_weights: tuple[int, int, int] = (4, 1, 1)
    rank_denominator: str = "admitted"
    admission_capacity: int | None = None
    newcomer_wave_counts: tuple[int, ...] | None = None
    newcomer_schedule_path: Path | None = None
    eligibility_path: Path | None = None
    eligibility_file_sha256: str | None = None
    # Fail closed: the fresh-start policy must not opt into over-cap sessions.
    allow_overcap_sessions: bool = False
    quest_discovery_budget: int = 8
    quest_renewal_budget: int = 1024
    quest_tick_interval_seconds: int = 1
    session_rotation_interval_seconds: int = 900
    session_rotation_max_per_tick: int = 1
    newcomer_session_share: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        if self.admission_capacity is None:
            object.__setattr__(self, "admission_capacity", self.durable_target)


def _integer(raw: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ArtifactError(f"{key} must be an integer in [{minimum}, {maximum}]")
    return value


def _digest(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactError(f"{key} must be a lowercase SHA-256")
    return value


def _absolute_path(raw: dict[str, Any], key: str) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ArtifactError(f"{key} must be a non-empty string")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactError(f"{key} must be absolute and contain no parent traversal")
    return path


def _load_bands(value: object, target: int) -> tuple[BandDefinition, ...]:
    if not isinstance(value, list) or len(value) != len(_BAND_NAMES):
        raise ArtifactError("bands must contain the five ordered owner-relative definitions")
    bands: list[BandDefinition] = []
    share_sum = Decimal(0)
    previous_offset = None
    for expected_name, row in zip(_BAND_NAMES, value, strict=True):
        if not isinstance(row, dict) or set(row) not in ({"name", "share", "owner_level_offset"}, {"name", "share", "owner_level_offset", "ceiling_mode", "minimum_slots"}):
            raise ArtifactError("each band must contain name, share, and owner_level_offset")
        if row["name"] != expected_name:
            raise ArtifactError("band names/order must be pioneer, scout, forward, main, trailing")
        try:
            share = Decimal(row["share"])
        except (InvalidOperation, TypeError) as exc:
            raise ArtifactError(f"band {expected_name} share is invalid") from exc
        exponent = share.as_tuple().exponent
        if not share.is_finite() or share <= 0 or not isinstance(exponent, int) or exponent < -2:
            raise ArtifactError(f"band {expected_name} share must be positive with at most 2 decimals")
        offset = row["owner_level_offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or not -79 <= offset <= 79:
            raise ArtifactError(f"band {expected_name} owner_level_offset is invalid")
        mode = row.get("ceiling_mode", "owner_relative")
        if mode not in ("phase_cap", "owner_relative") or (mode == "phase_cap" and expected_name != "pioneer"):
            raise ArtifactError("phase_cap is supported only for pioneer")
        if mode == "phase_cap" and offset != 0:
            raise ArtifactError("phase_cap ignores owner offset and requires zero")
        if mode == "owner_relative":
            if previous_offset is not None and offset <= previous_offset:
                raise ArtifactError("owner-relative offsets must strictly increase")
            previous_offset = offset
        minimum = row.get("minimum_slots", 0)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or not 0 <= minimum <= target:
            raise ArtifactError("minimum_slots must be an integer between 0 and durable_target")
        bands.append(BandDefinition(expected_name, share, offset, mode, minimum))
        share_sum += share
    if share_sum != Decimal(1):
        raise ArtifactError("band shares must sum to one")
    return tuple(bands)


def load_config(path: Path) -> RuntimeConfig:
    try:
        raw = load_json_object(path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    eligibility_keys = {"eligibility_path", "eligibility_file_sha256"}
    policy_keys = {
        "rank_denominator",
        "admission_capacity",
        "newcomer_schedule_path",
        "newcomer_schedule_sha256",
    }
    snapshot_keys = {
        "snapshot_weights",
        "pending_admission_limit",
        "allow_overcap_sessions",
        "quest_discovery_budget",
        "quest_renewal_budget",
        "quest_tick_interval_seconds",
        "session_rotation_interval_seconds",
        "session_rotation_max_per_tick",
        "newcomer_session_share",
    }
    if set(raw) - snapshot_keys not in (_EXPECTED_KEYS, _EXPECTED_KEYS | eligibility_keys, _EXPECTED_KEYS | policy_keys, _EXPECTED_KEYS | eligibility_keys | policy_keys):
        raise ArtifactError("runtime config keyset is not exact")
    if raw["schema_version"] != SCHEMA or raw["enabled"] is not True:
        raise ArtifactError("runtime config must be explicitly enabled with the supported schema")
    allow_overcap_sessions = raw.get("allow_overcap_sessions", False)
    if not isinstance(allow_overcap_sessions, bool):
        raise ArtifactError("allow_overcap_sessions must be a boolean")
    zones = raw["starter_zone_ids"]
    if (
        not isinstance(zones, list)
        or not zones
        or any(
            isinstance(zone, bool) or not isinstance(zone, int) or not 1 <= zone <= 65_535
            for zone in zones
        )
        or len(zones) != len(set(zones))
    ):
        raise ArtifactError("starter_zone_ids must be a non-empty unique integer list")
    target = raw.get("durable_target")
    if type(target) is not int or target < 1:
        raise ArtifactError("durable_target must be a positive integer")
    online_cap = _integer(raw, "online_cap", 1, target)
    if raw.get("rank_denominator", "admitted") not in ("admitted", "admission_capacity"):
        raise ArtifactError("rank_denominator must be admitted or admission_capacity")
    capacity = _integer(raw, "admission_capacity", 1, target) if "admission_capacity" in raw else target
    waves = None
    newcomer_path = None
    if "newcomer_schedule_path" in raw:
        newcomer_path = _absolute_path(raw, "newcomer_schedule_path")
        if newcomer_path.is_symlink() or digest_file(newcomer_path) != _digest(raw, "newcomer_schedule_sha256"):
            raise ArtifactError("newcomer schedule hash or regular-file binding mismatch")
        document = load_json_object(newcomer_path)
        if (set(document) != {"schema_version", "trigger", "identity_order", "admission_capacity", "decay", "counts"}
                or document["schema_version"] != "cpi194-owner-level-newcomers-v1"
                or document["trigger"] != "owner_level" or document["identity_order"] != "manifest_order"
                or document["admission_capacity"] != capacity):
            raise ArtifactError("newcomer schedule binding mismatch")
        waves = document["counts"]
    if waves is not None and (not isinstance(waves, list) or len(waves) != 80 or
            any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in waves) or
            waves[0] <= 0 or sum(waves) != capacity or any(a < b for a, b in zip(waves, waves[1:]))):
        raise ArtifactError("newcomer_wave_counts must be 80 nonnegative non-increasing counts summing to admission_capacity")
    weights = raw.get("snapshot_weights", {"active": 4, "uncached": 1, "offline": 1})
    if not isinstance(weights, dict) or set(weights) != {"active", "uncached", "offline"}:
        raise ArtifactError("snapshot_weights must name active, uncached and offline")
    snapshot_weights = tuple(_integer(weights, name, 1, 32) for name in ("active", "uncached", "offline"))
    rotation_interval = (
        _integer(raw, "session_rotation_interval_seconds", 1, 86_400)
        if "session_rotation_interval_seconds" in raw else 900
    )
    rotation_max = (
        _integer(raw, "session_rotation_max_per_tick", 1, 128)
        if "session_rotation_max_per_tick" in raw else 1
    )
    try:
        newcomer_share = Decimal(str(raw.get("newcomer_session_share", "0.10")))
    except (InvalidOperation, TypeError) as exc:
        raise ArtifactError("newcomer_session_share is invalid") from exc
    exponent = newcomer_share.as_tuple().exponent
    if (
        not newcomer_share.is_finite()
        or newcomer_share < 0
        or newcomer_share > 1
        or not isinstance(exponent, int)
        or exponent < -2
    ):
        raise ArtifactError("newcomer_session_share must be between 0 and 1 with at most 2 decimals")
    return RuntimeConfig(
        allow_overcap_sessions=allow_overcap_sessions,
        quest_discovery_budget=_integer(raw, "quest_discovery_budget", 1, 256) if "quest_discovery_budget" in raw else 8,
        quest_renewal_budget=_integer(raw, "quest_renewal_budget", 1, 10000) if "quest_renewal_budget" in raw else 1024,
        quest_tick_interval_seconds=_integer(raw, "quest_tick_interval_seconds", 1, 2) if "quest_tick_interval_seconds" in raw else 1,
        pending_admission_limit=_integer(raw, "pending_admission_limit", 1, target) if "pending_admission_limit" in raw else 1,
        snapshot_weights=snapshot_weights,
        manifest_path=_absolute_path(raw, "manifest_path"),
        manifest_file_sha256=_digest(raw, "manifest_file_sha256"),
        capabilities_path=_absolute_path(raw, "capabilities_path"),
        capabilities_file_sha256=_digest(raw, "capabilities_file_sha256"),
        identity_gate_path=_absolute_path(raw, "identity_gate_path"),
        identity_gate_sha256=_digest(raw, "identity_gate_sha256"),
        decisions_path=_absolute_path(raw, "decisions_path"),
        schedule_path=_absolute_path(raw, "schedule_path"),
        template_catalogue_path=_absolute_path(raw, "template_catalogue_path"),
        appearance_catalogue_path=_absolute_path(raw, "appearance_catalogue_path"),
        state_path=_absolute_path(raw, "state_path"),
        audit_path=_absolute_path(raw, "audit_path"),
        durable_target=target,
        online_cap=online_cap,
        admission_batch_size=_integer(raw, "admission_batch_size", 1, 500),
        admission_interval_seconds=_integer(raw, "admission_interval_seconds", 1, 604_800),
        bridge_batch_size=_integer(raw, "bridge_batch_size", 1, 256),
        action_batch_size=_integer(raw, "action_batch_size", 1, 128),
        snapshot_max_age_seconds=_integer(raw, "snapshot_max_age_seconds", 1, 300),
        starter_zone_ids=frozenset(zones),
        starter_zone_online_cap=_integer(raw, "starter_zone_online_cap", 1, target),
        bands=_load_bands(raw["bands"], target),
        rank_denominator=raw.get("rank_denominator", "admitted"),
        admission_capacity=capacity,
        newcomer_wave_counts=tuple(waves) if waves is not None else None,
        newcomer_schedule_path=newcomer_path,
        session_rotation_interval_seconds=rotation_interval,
        session_rotation_max_per_tick=rotation_max,
        newcomer_session_share=newcomer_share,
        eligibility_path=_absolute_path(raw, "eligibility_path") if "eligibility_path" in raw else None,
        eligibility_file_sha256=_digest(raw, "eligibility_file_sha256") if "eligibility_file_sha256" in raw else None,
    )
