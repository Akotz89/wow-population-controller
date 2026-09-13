"""Immutable installed-capability release gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .canonical import digest_file, digest_value, load_json_object
from .errors import ArtifactError

SCHEMA = "cpi-223-installed-population-capabilities-v1"
PORTS = (
    "population_apply_progression_policy",
    "population_bind_identity_once",
    "population_ensure_account",
    "population_ensure_character",
    "population_fail_closed",
    "population_read_capacity_snapshot",
    "population_read_earned_snapshot",
    "population_read_owner_high_water",
    "population_read_phase_snapshot",
    "population_release_session",
    "population_request_session",
    "population_reserve_identity",
    "population_set_admission",
)

# The planning seam is deliberately smaller than the complete future
# lifecycle bridge.  These ports only observe state; they cannot reserve an
# identity, create an account or character, bind a GUID, admit a row, or
# request/release a session.
PLANNING_PORTS = (
    "population_read_capacity_snapshot",
    "population_read_earned_snapshot",
    "population_read_owner_high_water",
    "population_read_phase_snapshot",
)
_ROOT_KEYS = {
    "schema_version",
    "status",
    "core_commit",
    "python_engine_commit",
    "playerbots_commit",
    "progression_system_commit",
    "bridge_profile",
    "ports",
    "generation",
    "manifest_digest",
}


@dataclass(frozen=True, slots=True)
class InstalledCapabilities:
    generation: int
    core_commit: str
    python_engine_commit: str
    playerbots_commit: str
    progression_system_commit: str
    manifest_digest: str
    bridge_profile: str = "general-hooks-with-optional-population-v1"
    ports: tuple[str, ...] = PORTS


def _commit(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactError(f"installed capability {name} must be an exact lowercase commit")
    return value


def load_capabilities(path: Path, expected_file_digest: str) -> InstalledCapabilities:
    if not path.is_file() or path.is_symlink():
        raise ArtifactError("installed capabilities must be a regular non-symlink file")
    if digest_file(path) != expected_file_digest:
        raise ArtifactError("installed capabilities file digest mismatch")
    try:
        raw = load_json_object(path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    if set(raw) != _ROOT_KEYS:
        raise ArtifactError("installed capabilities keyset is not exact")
    if raw["schema_version"] != SCHEMA or raw["status"] != "ACCEPTED":
        raise ArtifactError("installed capabilities are not an accepted v1 generation")
    if raw["bridge_profile"] != "general-hooks-with-optional-population-v1" or raw["ports"] != list(PORTS):
        raise ArtifactError("installed capabilities do not expose exactly population-v1")
    generation = raw["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ArtifactError("installed capability generation must be a positive integer")
    claimed = raw["manifest_digest"]
    payload = dict(raw)
    del payload["manifest_digest"]
    if not isinstance(claimed, str) or len(claimed) != 64 or digest_value(payload) != claimed:
        raise ArtifactError("installed capability manifest digest mismatch")
    return InstalledCapabilities(
        generation=generation,
        core_commit=_commit(raw["core_commit"], "core_commit"),
        python_engine_commit=_commit(raw["python_engine_commit"], "python_engine_commit"),
        playerbots_commit=_commit(raw["playerbots_commit"], "playerbots_commit"),
        progression_system_commit=_commit(raw["progression_system_commit"], "progression_system_commit"),
        manifest_digest=claimed,
        bridge_profile=raw["bridge_profile"],
        ports=tuple(raw["ports"]),
    )


def require_population_planning_capabilities(
    capabilities: InstalledCapabilities,
) -> tuple[str, ...]:
    """Require the exact installed bridge before any policy-only planning.

    The installed release may expose lifecycle ports for a later accepted
    binding implementation.  This function returns only the observation
    ports that the planning contract is allowed to use, and rejects a
    hand-built or stale capability record rather than silently downgrading to
    an unknown API surface.
    """

    if capabilities.bridge_profile != "general-hooks-with-optional-population-v1":
        raise ArtifactError("population planning requires the accepted population-v1 bridge")
    if capabilities.ports != PORTS:
        raise ArtifactError("population planning requires the exact population-v1 port set")
    if (
        len(capabilities.manifest_digest) != 64
        or any(character not in "0123456789abcdef" for character in capabilities.manifest_digest)
    ):
        raise ArtifactError("population planning capability manifest digest is invalid")
    return PLANNING_PORTS
