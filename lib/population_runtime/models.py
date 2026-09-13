"""Immutable policy and bridge value types."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class Identity:
    ordinal: int
    identity_key: str
    guid_allocation_key: str
    account_key: str
    account_name: str
    account_slot: int
    character_name: str
    faction: str
    race_id: int
    class_id: int
    role_intent: str
    spec_template_id: str
    starter_region: str
    wave_owner_level: int
    wave_ordinal: int
    initial_level: int
    appearance: dict[str, int]

    @property
    def wave_key(self) -> str:
        return f"owner-level-{self.wave_owner_level:02d}:{self.wave_ordinal:04d}"

    @property
    def immutable_creation_fields(self) -> dict[str, Any]:
        return {
            "character_name": self.character_name,
            "race_id": self.race_id,
            "class_id": self.class_id,
            "initial_level": self.initial_level,
            "appearance": dict(self.appearance),
        }


@dataclass(frozen=True, slots=True)
class IdentityBinding:
    identity_key: str
    account_id: int
    character_guid: int


@dataclass(frozen=True, slots=True)
class EarnedProgress:
    identity_key: str
    character_guid: int
    level: int
    xp: int
    played_seconds: int
    map_id: int
    zone_id: int
    session_requested: bool
    online: bool


@dataclass(frozen=True, slots=True)
class OwnerHighWater:
    character_guid: int
    level: int
    authenticated: bool
    observed_epoch: int


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    generation: int
    online_limit: int
    available_sessions: int
    valid: bool
    observed_epoch: int


@dataclass(frozen=True, slots=True)
class PhaseSnapshot:
    enabled: bool
    policy_valid: bool
    phase_id: str
    level_ceiling: int
    policy_digest: str


@dataclass(frozen=True, slots=True)
class BandDefinition:
    name: str
    share: Decimal
    owner_level_offset: int
    ceiling_mode: str = "owner_relative"
    minimum_slots: int = 0


@dataclass(frozen=True, slots=True)
class BandAssignment:
    identity_key: str
    character_guid: int
    band: str
    effective_ceiling: int
    earned_rank: int
