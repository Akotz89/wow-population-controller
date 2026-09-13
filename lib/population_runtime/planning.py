"""Read-only admission planning for a future accepted CPI-196 manifest.

The planner consumes an already validated identity manifest and observations
from the narrow native bridge.  It emits deterministic intents only; it does
not reserve an identity, create an account or character, bind a GUID, admit a
row, or request a session.  Those effects remain a later native/SQL gate.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from typing import Any

from .adapter import PopulationPlanningAdapter
from .canonical import digest_value
from .capabilities import InstalledCapabilities, require_population_planning_capabilities
from .errors import ArtifactError, BridgeError
from .manifest import IdentityManifest
from .models import CapacitySnapshot, OwnerHighWater, PhaseSnapshot

SCHEMA = "cpi-194-readonly-admission-plan-v1"
EXPECTED_PHASE_ID = "1_19"
EXPECTED_PHASE_CEILING = 19
_MAX_BATCH_SIZE = 128
_MAX_SNAPSHOT_AGE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class AdmissionIntent:
    """An identity-independent admission candidate with no binding fields."""

    identity_key: str
    manifest_digest: str
    wave_key: str
    wave_owner_level: int
    wave_ordinal: int


@dataclass(frozen=True, slots=True)
class PopulationPlan:
    """Canonical policy output that a future native binder may consume."""

    schema_version: str
    status: str
    manifest_digest: str
    capability_generation: int
    capacity_generation: int
    phase_policy_digest: str
    owner_level: int
    admitted_identity_count: int
    planned_identity_count: int
    owner_party_isolated: bool
    intents: tuple[AdmissionIntent, ...]
    plan_digest: str


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactError(f"{name} must be a lowercase SHA-256")
    return value


def _fresh(observed_epoch: int, now: int, maximum_age: int, name: str) -> None:
    if observed_epoch > now or now - observed_epoch > maximum_age:
        raise BridgeError(f"{name} snapshot is stale or from the future")


def _validate_manifest_shape(manifest: IdentityManifest) -> tuple[str, ...]:
    _digest(manifest.manifest_digest, "identity manifest digest")
    if not manifest.identities:
        raise ArtifactError("identity manifest must contain at least one accepted identity")
    keys = tuple(identity.identity_key for identity in manifest.identities)
    if any(not isinstance(key, str) or not key for key in keys):
        raise ArtifactError("identity manifest contains an empty identity key")
    if len(keys) != len(set(keys)):
        raise ArtifactError("identity manifest contains duplicate identity keys")
    for identity in manifest.identities:
        if any(hasattr(identity, field) for field in ("owner_guid", "owner_account_id", "owner_character_guid")):
            raise ArtifactError("identity manifest row contains a forbidden owner identity field")
        if (
            not isinstance(identity.wave_owner_level, int)
            or isinstance(identity.wave_owner_level, bool)
            or not 1 <= identity.wave_owner_level <= 80
            or not isinstance(identity.wave_ordinal, int)
            or isinstance(identity.wave_ordinal, bool)
            or identity.wave_ordinal <= 0
        ):
            raise ArtifactError("identity manifest contains an invalid wave coordinate")
    return keys


def _validate_admitted(
    admitted_identity_keys: Collection[str], manifest_keys: set[str]
) -> set[str]:
    admitted = tuple(admitted_identity_keys)
    if any(not isinstance(key, str) or not key for key in admitted):
        raise ArtifactError("admitted identity keys must be non-empty strings")
    if len(admitted) != len(set(admitted)):
        raise ArtifactError("admitted identity keys must be unique")
    if not set(admitted) <= manifest_keys:
        raise BridgeError("admitted identity state contains a key outside the accepted manifest")
    return set(admitted)


def _validate_observations(
    owner: OwnerHighWater,
    capacity: CapacitySnapshot,
    phase: PhaseSnapshot,
    now: int,
    maximum_snapshot_age: int,
) -> None:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ArtifactError("planner clock must be a non-negative integer")
    if (
        isinstance(maximum_snapshot_age, bool)
        or not isinstance(maximum_snapshot_age, int)
        or not 1 <= maximum_snapshot_age <= _MAX_SNAPSHOT_AGE_SECONDS
    ):
        raise ArtifactError("maximum snapshot age is outside the bounded range")
    _fresh(owner.observed_epoch, now, maximum_snapshot_age, "owner")
    _fresh(capacity.observed_epoch, now, maximum_snapshot_age, "capacity")
    if (
        not owner.authenticated
        or owner.character_guid <= 0
        or not 1 <= owner.level <= 80
    ):
        raise BridgeError("owner high-water observation is not authenticated and valid")
    if (
        not capacity.valid
        or capacity.generation <= 0
        or capacity.online_limit <= 0
        or capacity.available_sessions < 0
        or capacity.available_sessions > capacity.online_limit
    ):
        raise BridgeError("capacity observation is invalid")
    if (
        not phase.enabled
        or not phase.policy_valid
        or phase.phase_id != EXPECTED_PHASE_ID
        or phase.level_ceiling != EXPECTED_PHASE_CEILING
    ):
        raise BridgeError("shared phase observation is not the valid 1_19/19 gate")
    _digest(phase.policy_digest, "phase policy digest")
    if owner.level > phase.level_ceiling:
        raise BridgeError("owner high-water exceeds the shared phase ceiling")


def _payload(plan: PopulationPlan) -> dict[str, Any]:
    return {
        "schema_version": plan.schema_version,
        "status": plan.status,
        "manifest_digest": plan.manifest_digest,
        "capability_generation": plan.capability_generation,
        "capacity_generation": plan.capacity_generation,
        "phase_policy_digest": plan.phase_policy_digest,
        "owner_level": plan.owner_level,
        "admitted_identity_count": plan.admitted_identity_count,
        "planned_identity_count": plan.planned_identity_count,
        "owner_party_isolated": plan.owner_party_isolated,
        "intents": [
            {
                "identity_key": intent.identity_key,
                "manifest_digest": intent.manifest_digest,
                "wave_key": intent.wave_key,
                "wave_owner_level": intent.wave_owner_level,
                "wave_ordinal": intent.wave_ordinal,
            }
            for intent in plan.intents
        ],
    }


def population_plan_document(plan: PopulationPlan) -> dict[str, Any]:
    """Return the canonical JSON-compatible plan, including its digest."""

    document = _payload(plan)
    document["plan_digest"] = plan.plan_digest
    return document


def build_readonly_admission_plan(
    manifest: IdentityManifest,
    capabilities: InstalledCapabilities,
    owner: OwnerHighWater,
    capacity: CapacitySnapshot,
    phase: PhaseSnapshot,
    admitted_identity_keys: Collection[str],
    now: int,
    *,
    batch_size: int,
    maximum_snapshot_age: int = 30,
) -> PopulationPlan:
    """Build a bounded policy plan without invoking any native mutator port."""

    require_population_planning_capabilities(capabilities)
    manifest_keys = set(_validate_manifest_shape(manifest))
    admitted = _validate_admitted(admitted_identity_keys, manifest_keys)
    _validate_observations(owner, capacity, phase, now, maximum_snapshot_age)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= _MAX_BATCH_SIZE:
        raise ArtifactError("batch_size is outside the bounded planning range")

    ordered = sorted(
        manifest.identities,
        key=lambda identity: (
            identity.wave_owner_level,
            identity.wave_ordinal,
            identity.ordinal,
            identity.identity_key,
        ),
    )
    remaining = [
        identity
        for identity in ordered
        if identity.identity_key not in admitted and identity.wave_owner_level <= owner.level
    ]
    status = "EMPTY"
    candidates = remaining
    if remaining and capacity.available_sessions == 0:
        status = "HELD_CAPACITY"
        candidates = []
    elif remaining:
        first_wave = remaining[0].wave_owner_level
        candidates = [identity for identity in remaining if identity.wave_owner_level == first_wave]
        status = "READY"

    intents = tuple(
        AdmissionIntent(
            identity_key=identity.identity_key,
            manifest_digest=manifest.manifest_digest,
            wave_key=identity.wave_key,
            wave_owner_level=identity.wave_owner_level,
            wave_ordinal=identity.wave_ordinal,
        )
        for identity in candidates[:batch_size]
    )
    plan = PopulationPlan(
        schema_version=SCHEMA,
        status=status,
        manifest_digest=manifest.manifest_digest,
        capability_generation=capabilities.generation,
        capacity_generation=capacity.generation,
        phase_policy_digest=phase.policy_digest,
        owner_level=owner.level,
        admitted_identity_count=len(admitted),
        planned_identity_count=len(intents),
        owner_party_isolated=True,
        intents=intents,
        plan_digest="",
    )
    payload = _payload(plan)
    return replace(plan, plan_digest=digest_value(payload))


def build_readonly_plan_from_adapter(
    manifest: IdentityManifest,
    capabilities: InstalledCapabilities,
    adapter: PopulationPlanningAdapter,
    admitted_identity_keys: Collection[str],
    now: int,
    *,
    batch_size: int,
    maximum_snapshot_age: int = 30,
) -> PopulationPlan:
    """Collect only observations, then produce the same deterministic plan."""

    return build_readonly_admission_plan(
        manifest,
        capabilities,
        adapter.read_owner_high_water(),
        adapter.read_capacity_snapshot(),
        adapter.read_phase_snapshot(),
        admitted_identity_keys,
        now,
        batch_size=batch_size,
        maximum_snapshot_age=maximum_snapshot_age,
    )
