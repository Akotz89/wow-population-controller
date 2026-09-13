"""Offline durable identity lifecycle and compare-and-swap contract.

The runtime controller talks to a narrow native bridge, but the bridge still
needs a precise contract for the durable identity ledger it will eventually
implement.  This module is deliberately side-effect free apart from its
explicit atomic JSON helpers: it does not open a database, create an account,
or admit a session.

The identity lifecycle is forward-only through ``active``.  Session demand is
a separate mutable lease on an active identity, so releasing a session never
undoes account, character, RandomBot, or admission facts.  Every mutation is
bound to the manifest digest and a per-identity generation.  A retry with the
same event intent is idempotent; a stale or conflicting compare-and-swap is a
fail-closed ``LedgerConflict``.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest_value, load_json_object
from .errors import ArtifactError, LedgerConflict, LedgerError
from .models import Identity

SCHEMA = "cpi-194-population-identity-ledger-v1"

LIFECYCLE_STATES = (
    "reserved",
    "account_pending",
    "account_verified",
    "character_pending",
    "character_verified",
    "rndbot_pending",
    "binding_verified",
    "active",
    "blocked_conflict",
)
_FORWARD_TRANSITIONS: dict[str, frozenset[str]] = {
    "reserved": frozenset({"account_pending", "blocked_conflict"}),
    "account_pending": frozenset({"account_verified", "blocked_conflict"}),
    "account_verified": frozenset({"character_pending", "blocked_conflict"}),
    "character_pending": frozenset({"character_verified", "blocked_conflict"}),
    "character_verified": frozenset({"rndbot_pending", "blocked_conflict"}),
    "rndbot_pending": frozenset({"binding_verified", "blocked_conflict"}),
    "binding_verified": frozenset({"active", "blocked_conflict"}),
    "active": frozenset({"blocked_conflict"}),
    "blocked_conflict": frozenset(),
}
_IDENTITY_KEYS = {
    "manifest_digest",
    "identity_key",
    "guid_allocation_key",
    "account_key",
    "account_name",
    "account_slot",
    "character_name",
    "wave_key",
    "expected_identity_digest",
}
_ROW_KEYS = {
    "identity",
    "state",
    "generation",
    "account_id",
    "character_guid",
    "session_requested",
    "capacity_generation",
    "last_event_key",
}
_EVENT_KEYS = {
    "event_key",
    "event_type",
    "manifest_digest",
    "identity_key",
    "expected_generation",
    "next_generation",
    "from_state",
    "to_state",
    "account_id",
    "character_guid",
    "session_requested",
    "capacity_generation",
    "evidence_digest",
    "payload_digest",
}
_LEDGER_KEYS = {"schema_version", "manifest_digest", "revision", "rows", "audit", "ledger_digest"}
_ZERO_DIGEST = "0" * 64


@dataclass(frozen=True, slots=True)
class LedgerIdentity:
    """Immutable identity facts copied from the accepted manifest row."""

    manifest_digest: str
    identity_key: str
    guid_allocation_key: str
    account_key: str
    account_name: str
    account_slot: int
    character_name: str
    wave_key: str
    expected_identity_digest: str


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """Current durable facts for one immutable identity."""

    identity: LedgerIdentity
    state: str
    generation: int
    account_id: int | None = None
    character_guid: int | None = None
    session_requested: bool = False
    capacity_generation: int | None = None
    last_event_key: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """One append-only, hash-bound compare-and-swap result."""

    event_key: str
    event_type: str
    manifest_digest: str
    identity_key: str
    expected_generation: int
    next_generation: int
    from_state: str
    to_state: str
    account_id: int | None
    character_guid: int | None
    session_requested: bool
    capacity_generation: int | None
    evidence_digest: str
    payload_digest: str


@dataclass(slots=True)
class DurableLedger:
    """A digest-bound ledger suitable for offline replay and bridge handoff."""

    manifest_digest: str
    revision: int = 0
    rows: dict[str, LedgerRow] = field(default_factory=dict)
    audit: list[LedgerEvent] = field(default_factory=list)


def _require_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LedgerError(f"{name} must be a lowercase SHA-256")
    return value


def _require_key(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise LedgerError(f"{name} must be a non-empty token of at most 128 characters")
    return value


def _positive_or_none(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LedgerError(f"{name} must be null or a positive integer")
    return value


def _nonnegative(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerError(f"{name} must be a non-negative integer")
    return value


def _identity_payload(identity: Identity) -> dict[str, Any]:
    """Return every manifest field that is relevant to immutable identity."""

    # ``asdict`` keeps nested appearance values in the digest and avoids
    # silently dropping a future immutable manifest field in this development
    # contract.  The manifest loader still owns the authoritative row shape.
    return asdict(identity)


def identity_from_manifest(identity: Identity, manifest_digest: str) -> LedgerIdentity:
    """Project one accepted manifest row into the durable ledger identity."""

    _require_digest(manifest_digest, "manifest_digest")
    fields = _identity_payload(identity)
    expected_digest = digest_value({"manifest_digest": manifest_digest, "identity": fields})
    result = LedgerIdentity(
        manifest_digest=manifest_digest,
        identity_key=_require_key(identity.identity_key, "identity.identity_key"),
        guid_allocation_key=_require_key(
            identity.guid_allocation_key, "identity.guid_allocation_key"
        ),
        account_key=_require_key(identity.account_key, "identity.account_key"),
        account_name=_require_key(identity.account_name, "identity.account_name"),
        account_slot=identity.account_slot,
        character_name=_require_key(identity.character_name, "identity.character_name"),
        wave_key=_require_key(identity.wave_key, "identity.wave_key"),
        expected_identity_digest=expected_digest,
    )
    if (
        isinstance(identity.account_slot, bool)
        or not isinstance(identity.account_slot, int)
        or identity.account_slot <= 0
    ):
        raise LedgerError("identity.account_slot must be positive")
    return result


def new_ledger(manifest_digest: str) -> DurableLedger:
    """Create an empty ledger; no identity is authorized by this operation."""

    _require_digest(manifest_digest, "manifest_digest")
    return DurableLedger(manifest_digest=manifest_digest)


def _validate_identity(identity: LedgerIdentity, manifest_digest: str) -> None:
    _require_digest(manifest_digest, "manifest_digest")
    if identity.manifest_digest != manifest_digest:
        raise LedgerConflict("identity manifest digest conflicts with the ledger")
    for name in (
        "identity_key",
        "guid_allocation_key",
        "account_key",
        "account_name",
        "character_name",
        "wave_key",
    ):
        _require_key(getattr(identity, name), f"identity.{name}")
    if identity.account_slot <= 0:
        raise LedgerError("identity.account_slot must be positive")
    _require_digest(identity.expected_identity_digest, "identity.expected_identity_digest")


def _validate_row(row: LedgerRow, manifest_digest: str) -> None:
    _validate_identity(row.identity, manifest_digest)
    if row.state not in LIFECYCLE_STATES:
        raise LedgerError(f"unsupported ledger state: {row.state}")
    if isinstance(row.generation, bool) or not isinstance(row.generation, int) or row.generation <= 0:
        raise LedgerError("ledger row generation must be positive")
    account_id = _positive_or_none(row.account_id, "account_id")
    character_guid = _positive_or_none(row.character_guid, "character_guid")
    if character_guid is not None and account_id is None:
        raise LedgerError("character_guid requires account_id")
    if row.state in {
        "account_verified",
        "character_pending",
        "character_verified",
        "rndbot_pending",
        "binding_verified",
        "active",
    } and account_id is None:
        raise LedgerError(f"{row.state} requires account_id")
    if (
        row.state in {"character_verified", "rndbot_pending", "binding_verified", "active"}
        and character_guid is None
    ):
        raise LedgerError(f"{row.state} requires character_guid")
    if not isinstance(row.session_requested, bool):
        raise LedgerError("session_requested must be boolean")
    if row.state != "active" and row.session_requested:
        raise LedgerError("only an active identity may request a session")
    if row.session_requested:
        if _positive_or_none(row.capacity_generation, "capacity_generation") is None:
            raise LedgerError("session_requested requires capacity_generation")
    elif row.capacity_generation is not None:
        raise LedgerError("capacity_generation must be null without a requested session")
    if row.state == "blocked_conflict" and row.session_requested:
        raise LedgerError("blocked identities cannot retain a requested session")
    if row.last_event_key is not None:
        _require_digest(row.last_event_key, "last_event_key")


def _same_identity(left: LedgerIdentity, right: LedgerIdentity) -> bool:
    return left == right


def _find_event(
    ledger: DurableLedger,
    identity_key: str,
    event_type: str,
    expected_generation: int,
    to_state: str,
    account_id: int | None,
    character_guid: int | None,
    session_requested: bool,
    capacity_generation: int | None,
    evidence_digest: str,
) -> LedgerEvent | None:
    for event in reversed(ledger.audit):
        if (
            event.identity_key == identity_key
            and event.event_type == event_type
            and event.expected_generation == expected_generation
            and event.to_state == to_state
            and event.account_id == account_id
            and event.character_guid == character_guid
            and event.session_requested == session_requested
            and event.capacity_generation == capacity_generation
            and event.evidence_digest == evidence_digest
        ):
            return event
    return None


def _event_payload(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    event_type: str,
    expected_generation: int,
    next_generation: int,
    from_state: str,
    to_state: str,
    account_id: int | None,
    character_guid: int | None,
    session_requested: bool,
    capacity_generation: int | None,
    evidence_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "event_type": event_type,
        "manifest_digest": ledger.manifest_digest,
        "identity_key": identity.identity_key,
        "identity_digest": identity.expected_identity_digest,
        "expected_generation": expected_generation,
        "next_generation": next_generation,
        "from_state": from_state,
        "to_state": to_state,
        "account_id": account_id,
        "character_guid": character_guid,
        "session_requested": session_requested,
        "capacity_generation": capacity_generation,
        "evidence_digest": evidence_digest,
    }


def _make_event(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    event_type: str,
    expected_generation: int,
    next_generation: int,
    from_state: str,
    to_state: str,
    account_id: int | None,
    character_guid: int | None,
    session_requested: bool,
    capacity_generation: int | None,
    evidence_digest: str,
) -> LedgerEvent:
    payload = _event_payload(
        ledger,
        identity,
        event_type,
        expected_generation,
        next_generation,
        from_state,
        to_state,
        account_id,
        character_guid,
        session_requested,
        capacity_generation,
        evidence_digest,
    )
    payload_digest = digest_value(payload)
    event_key = digest_value({"payload_digest": payload_digest, "payload": payload})
    return LedgerEvent(
        event_key=event_key,
        event_type=event_type,
        manifest_digest=ledger.manifest_digest,
        identity_key=identity.identity_key,
        expected_generation=expected_generation,
        next_generation=next_generation,
        from_state=from_state,
        to_state=to_state,
        account_id=account_id,
        character_guid=character_guid,
        session_requested=session_requested,
        capacity_generation=capacity_generation,
        evidence_digest=evidence_digest,
        payload_digest=payload_digest,
    )


def _commit(
    ledger: DurableLedger,
    row: LedgerRow | None,
    identity: LedgerIdentity,
    event_type: str,
    expected_generation: int,
    to_state: str,
    account_id: int | None,
    character_guid: int | None,
    session_requested: bool,
    capacity_generation: int | None,
    evidence_digest: str,
) -> LedgerEvent:
    _validate_identity(identity, ledger.manifest_digest)
    _require_digest(evidence_digest, "evidence_digest")
    if row is not None:
        _validate_row(row, ledger.manifest_digest)
        if not _same_identity(row.identity, identity):
            raise LedgerConflict("immutable identity fields conflict with the existing ledger row")
        from_state = row.state
        next_generation = row.generation + 1
        existing = _find_event(
            ledger,
            identity.identity_key,
            event_type,
            expected_generation,
            to_state,
            account_id,
            character_guid,
            session_requested,
            capacity_generation,
            evidence_digest,
        )
        if existing is not None:
            return existing
        if from_state == to_state and event_type != "session":
            raise LedgerConflict(f"no-op lifecycle transition for {identity.identity_key}")
        if event_type == "session" and row.session_requested == session_requested:
            raise LedgerConflict(f"no-op session transition for {identity.identity_key}")
        if row.generation != expected_generation:
            raise LedgerConflict(
                f"stale generation for {identity.identity_key}: expected "
                f"{expected_generation}, observed {row.generation}"
            )
    else:
        from_state = "absent"
        next_generation = 1
        if expected_generation != 0:
            raise LedgerConflict("a new identity reservation requires expected_generation=0")
        existing = _find_event(
            ledger,
            identity.identity_key,
            event_type,
            expected_generation,
            to_state,
            account_id,
            character_guid,
            session_requested,
            capacity_generation,
            evidence_digest,
        )
        if existing is not None:
            return existing

    if next_generation <= 0:
        raise LedgerError("ledger generation overflowed")
    event = _make_event(
        ledger,
        identity,
        event_type,
        expected_generation,
        next_generation,
        from_state,
        to_state,
        account_id,
        character_guid,
        session_requested,
        capacity_generation,
        evidence_digest,
    )
    replacement = LedgerRow(
        identity=identity,
        state=to_state,
        generation=next_generation,
        account_id=account_id,
        character_guid=character_guid,
        session_requested=session_requested,
        capacity_generation=capacity_generation,
        last_event_key=event.event_key,
    )
    _validate_row(replacement, ledger.manifest_digest)
    ledger.rows[identity.identity_key] = replacement
    ledger.audit.append(event)
    ledger.revision += 1
    return event


def reserve_identity(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    evidence_digest: str = _ZERO_DIGEST,
) -> LedgerEvent:
    """Reserve an identity exactly once using the absent-to-reserved CAS."""

    existing = ledger.rows.get(identity.identity_key)
    if existing is not None:
        _validate_row(existing, ledger.manifest_digest)
        if not _same_identity(existing.identity, identity):
            raise LedgerConflict("reservation collides with immutable identity facts")
        for event in ledger.audit:
            if event.identity_key == identity.identity_key and event.event_type == "reserve":
                if event.evidence_digest == evidence_digest:
                    return event
                raise LedgerConflict("reservation evidence conflicts with the existing audit event")
        raise LedgerConflict("existing identity row has no auditable reservation event")
    return _commit(
        ledger,
        None,
        identity,
        "reserve",
        0,
        "reserved",
        None,
        None,
        False,
        None,
        evidence_digest,
    )


def transition_identity(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    to_state: str,
    expected_generation: int,
    *,
    account_id: int | None = None,
    character_guid: int | None = None,
    evidence_digest: str = _ZERO_DIGEST,
) -> LedgerEvent:
    """Apply one forward-only lifecycle CAS transition."""

    if to_state not in LIFECYCLE_STATES or to_state == "reserved":
        raise LedgerError(f"unsupported lifecycle target: {to_state}")
    row = ledger.rows.get(identity.identity_key)
    if row is None:
        raise LedgerConflict("lifecycle transition requires a prior reservation")
    if row.state == "blocked_conflict":
        existing = _find_event(
            ledger,
            identity.identity_key,
            "lifecycle",
            expected_generation,
            to_state,
            account_id,
            character_guid,
            False,
            None,
            evidence_digest,
        )
        if existing is not None:
            return existing
        raise LedgerConflict("blocked identity cannot transition")
    if to_state not in _FORWARD_TRANSITIONS[row.state]:
        raise LedgerConflict(f"illegal lifecycle transition {row.state} -> {to_state}")
    effective_account = row.account_id if account_id is None else _positive_or_none(account_id, "account_id")
    effective_character = (
        row.character_guid
        if character_guid is None
        else _positive_or_none(character_guid, "character_guid")
    )
    return _commit(
        ledger,
        row,
        identity,
        "lifecycle",
        expected_generation,
        to_state,
        effective_account,
        effective_character,
        False,
        None,
        evidence_digest,
    )


def block_conflict(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    expected_generation: int,
    *,
    evidence_digest: str = _ZERO_DIGEST,
) -> LedgerEvent:
    """Record a terminal conflict without deleting or rewriting the identity."""

    row = ledger.rows.get(identity.identity_key)
    if row is None:
        raise LedgerConflict("cannot block an unreserved identity")
    if row.state == "blocked_conflict":
        existing = _find_event(
            ledger,
            identity.identity_key,
            "lifecycle",
            expected_generation,
            "blocked_conflict",
            row.account_id,
            row.character_guid,
            False,
            None,
            evidence_digest,
        )
        if existing is not None:
            return existing
        raise LedgerConflict("blocked identity is terminal")
    return _commit(
        ledger,
        row,
        identity,
        "lifecycle",
        expected_generation,
        "blocked_conflict",
        row.account_id,
        row.character_guid,
        False,
        None,
        evidence_digest,
    )


def set_session_requested(
    ledger: DurableLedger,
    identity: LedgerIdentity,
    expected_generation: int,
    requested: bool,
    *,
    capacity_generation: int | None = None,
    evidence_digest: str = _ZERO_DIGEST,
) -> LedgerEvent:
    """CAS a session lease on an active identity without changing lifecycle facts."""

    row = ledger.rows.get(identity.identity_key)
    if row is None:
        raise LedgerConflict("session lease requires a prior reservation")
    if row.state != "active":
        raise LedgerConflict("only an active identity may request or release a session")
    capacity = _positive_or_none(capacity_generation, "capacity_generation") if requested else None
    if requested and capacity is None:
        raise LedgerError("requested session requires a positive capacity_generation")
    if not requested and capacity_generation is not None:
        raise LedgerError("released session must not carry capacity_generation")
    return _commit(
        ledger,
        row,
        identity,
        "session",
        expected_generation,
        "active",
        row.account_id,
        row.character_guid,
        requested,
        capacity,
        evidence_digest,
    )


def _identity_from_raw(raw: object) -> LedgerIdentity:
    if not isinstance(raw, dict) or set(raw) != _IDENTITY_KEYS:
        raise ArtifactError("ledger identity keyset is invalid")
    account_slot = raw["account_slot"]
    if isinstance(account_slot, bool) or not isinstance(account_slot, int) or account_slot <= 0:
        raise ArtifactError("ledger identity account_slot is invalid")
    values = {
        key: _require_key(raw[key], f"ledger identity {key}")
        for key in (
            "identity_key",
            "guid_allocation_key",
            "account_key",
            "account_name",
            "character_name",
            "wave_key",
        )
    }
    identity = LedgerIdentity(
        manifest_digest=_require_digest(raw["manifest_digest"], "ledger identity manifest_digest"),
        account_slot=account_slot,
        expected_identity_digest=_require_digest(
            raw["expected_identity_digest"], "ledger identity expected_identity_digest"
        ),
        **values,
    )
    return identity


def _row_to_payload(row: LedgerRow) -> dict[str, Any]:
    return {
        "identity": asdict(row.identity),
        "state": row.state,
        "generation": row.generation,
        "account_id": row.account_id,
        "character_guid": row.character_guid,
        "session_requested": row.session_requested,
        "capacity_generation": row.capacity_generation,
        "last_event_key": row.last_event_key,
    }


def _row_from_raw(raw: object, manifest_digest: str) -> LedgerRow:
    if not isinstance(raw, dict) or set(raw) != _ROW_KEYS:
        raise ArtifactError("ledger row keyset is invalid")
    generation = raw["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ArtifactError("ledger row generation is invalid")
    account_id = _positive_or_none(raw["account_id"], "ledger row account_id")
    character_guid = _positive_or_none(raw["character_guid"], "ledger row character_guid")
    session_requested = raw["session_requested"]
    if not isinstance(session_requested, bool):
        raise ArtifactError("ledger row session_requested is invalid")
    capacity_generation = _positive_or_none(
        raw["capacity_generation"], "ledger row capacity_generation"
    )
    last_event_key = raw["last_event_key"]
    if last_event_key is not None:
        last_event_key = _require_digest(last_event_key, "ledger row last_event_key")
    identity = _identity_from_raw(raw["identity"])
    row = LedgerRow(
        identity=identity,
        state=raw["state"],
        generation=generation,
        account_id=account_id,
        character_guid=character_guid,
        session_requested=session_requested,
        capacity_generation=capacity_generation,
        last_event_key=last_event_key,
    )
    try:
        _validate_row(row, manifest_digest)
    except LedgerError as exc:
        raise ArtifactError(str(exc)) from exc
    return row


def _event_from_raw(raw: object) -> LedgerEvent:
    if not isinstance(raw, dict) or set(raw) != _EVENT_KEYS:
        raise ArtifactError("ledger audit event keyset is invalid")
    event_key = _require_digest(raw["event_key"], "ledger event_key")
    manifest_digest = _require_digest(raw["manifest_digest"], "ledger event manifest_digest")
    identity_key = _require_key(raw["identity_key"], "ledger event identity_key")
    event_type = raw["event_type"]
    if event_type not in {"reserve", "lifecycle", "session"}:
        raise ArtifactError("ledger event_type is invalid")
    expected_generation = raw["expected_generation"]
    next_generation = raw["next_generation"]
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
        or isinstance(next_generation, bool)
        or not isinstance(next_generation, int)
        or next_generation != expected_generation + 1
    ):
        raise ArtifactError("ledger event generations are invalid")
    from_state = raw["from_state"]
    to_state = raw["to_state"]
    if not isinstance(from_state, str) or not isinstance(to_state, str):
        raise ArtifactError("ledger event states are invalid")
    account_id = _positive_or_none(raw["account_id"], "ledger event account_id")
    character_guid = _positive_or_none(raw["character_guid"], "ledger event character_guid")
    session_requested = raw["session_requested"]
    if not isinstance(session_requested, bool):
        raise ArtifactError("ledger event session_requested is invalid")
    capacity_generation = _positive_or_none(
        raw["capacity_generation"], "ledger event capacity_generation"
    )
    evidence_digest = _require_digest(raw["evidence_digest"], "ledger event evidence_digest")
    payload_digest = _require_digest(raw["payload_digest"], "ledger event payload_digest")
    return LedgerEvent(
        event_key=event_key,
        event_type=event_type,
        manifest_digest=manifest_digest,
        identity_key=identity_key,
        expected_generation=expected_generation,
        next_generation=next_generation,
        from_state=from_state,
        to_state=to_state,
        account_id=account_id,
        character_guid=character_guid,
        session_requested=session_requested,
        capacity_generation=capacity_generation,
        evidence_digest=evidence_digest,
        payload_digest=payload_digest,
    )


def _payload(ledger: DurableLedger) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "manifest_digest": ledger.manifest_digest,
        "revision": ledger.revision,
        "rows": {key: _row_to_payload(row) for key, row in sorted(ledger.rows.items())},
        "audit": [asdict(event) for event in ledger.audit],
    }


def _validate_audit(ledger: DurableLedger) -> None:
    if ledger.revision != len(ledger.audit):
        raise ArtifactError("ledger revision must equal the append-only audit length")
    seen: set[str] = set()
    by_identity_generation: dict[str, int] = {}
    by_identity_state: dict[str, str] = {}
    by_identity_account: dict[str, int | None] = {}
    by_identity_character: dict[str, int | None] = {}
    by_identity_session: dict[str, bool] = {}
    by_identity_capacity: dict[str, int | None] = {}
    for event in ledger.audit:
        if event.event_key in seen:
            raise ArtifactError("ledger audit contains a duplicate event key")
        seen.add(event.event_key)
        if event.manifest_digest != ledger.manifest_digest:
            raise ArtifactError("ledger audit event has another manifest digest")
        identity = ledger.rows.get(event.identity_key)
        if identity is None:
            raise ArtifactError("ledger audit references an absent identity row")
        _validate_digest_event(event, identity.identity)
        previous_generation = by_identity_generation.get(event.identity_key, 0)
        if event.expected_generation != previous_generation:
            raise ArtifactError("ledger audit generations are not contiguous")
        previous_state = by_identity_state.get(event.identity_key, "absent")
        if event.from_state != previous_state:
            raise ArtifactError("ledger audit state chain is not contiguous")
        previous_account = by_identity_account.get(event.identity_key)
        previous_character = by_identity_character.get(event.identity_key)
        previous_session = by_identity_session.get(event.identity_key, False)
        if event.event_type == "reserve":
            if (
                event.from_state != "absent"
                or event.to_state != "reserved"
                or event.expected_generation != 0
                or event.account_id is not None
                or event.character_guid is not None
                or event.session_requested
                or event.capacity_generation is not None
            ):
                raise ArtifactError("ledger reservation event semantics are invalid")
        elif event.event_type == "lifecycle":
            if event.from_state == "absent" or event.to_state not in LIFECYCLE_STATES:
                raise ArtifactError("ledger lifecycle event states are invalid")
            if event.from_state == event.to_state:
                raise ArtifactError("ledger lifecycle event must change state")
            if event.to_state != "blocked_conflict" and event.to_state not in _FORWARD_TRANSITIONS[
                event.from_state
            ]:
                raise ArtifactError("ledger lifecycle event skips or reverses a state")
            if event.session_requested or event.capacity_generation is not None:
                raise ArtifactError("lifecycle event cannot carry a session lease")
            if previous_account is not None and event.account_id != previous_account:
                raise ArtifactError("ledger lifecycle event changed account_id")
            if previous_character is not None and event.character_guid != previous_character:
                raise ArtifactError("ledger lifecycle event changed character_guid")
        elif event.event_type == "session":
            if (
                event.from_state != "active"
                or event.to_state != "active"
                or previous_state != "active"
                or event.session_requested == previous_session
            ):
                raise ArtifactError("ledger session event semantics are invalid")
            if event.account_id != previous_account or event.character_guid != previous_character:
                raise ArtifactError("ledger session event changed identity bindings")
            if event.session_requested != (event.capacity_generation is not None):
                raise ArtifactError("ledger session event capacity lease is invalid")
        else:
            raise ArtifactError("ledger audit event type is invalid")
        candidate = LedgerRow(
            identity=identity.identity,
            state=event.to_state,
            generation=event.next_generation,
            account_id=event.account_id,
            character_guid=event.character_guid,
            session_requested=event.session_requested,
            capacity_generation=event.capacity_generation,
            last_event_key=event.event_key,
        )
        try:
            _validate_row(candidate, ledger.manifest_digest)
        except LedgerError as exc:
            raise ArtifactError(str(exc)) from exc
        by_identity_generation[event.identity_key] = event.next_generation
        by_identity_state[event.identity_key] = event.to_state
        by_identity_account[event.identity_key] = event.account_id
        by_identity_character[event.identity_key] = event.character_guid
        by_identity_session[event.identity_key] = event.session_requested
        by_identity_capacity[event.identity_key] = event.capacity_generation
    for key, row in ledger.rows.items():
        if key != row.identity.identity_key:
            raise ArtifactError("ledger row key does not match identity_key")
        if row.last_event_key is None:
            raise ArtifactError("ledger row is missing its last_event_key")
        if row.last_event_key not in seen:
            raise ArtifactError("ledger row last_event_key is absent from the audit")
        if by_identity_generation.get(key) != row.generation:
            raise ArtifactError("ledger row generation does not match its audit chain")
        if by_identity_state.get(key) != row.state:
            raise ArtifactError("ledger row state does not match its audit chain")
        if by_identity_account.get(key) != row.account_id:
            raise ArtifactError("ledger row account_id does not match its audit chain")
        if by_identity_character.get(key) != row.character_guid:
            raise ArtifactError("ledger row character_guid does not match its audit chain")
        if by_identity_session.get(key) != row.session_requested:
            raise ArtifactError("ledger row session lease does not match its audit chain")
        if by_identity_capacity.get(key) != row.capacity_generation:
            raise ArtifactError("ledger row capacity generation does not match its audit chain")


def _validate_digest_event(event: LedgerEvent, identity: LedgerIdentity) -> None:
    payload = _event_payload(
        DurableLedger(event.manifest_digest),
        identity,
        event.event_type,
        event.expected_generation,
        event.next_generation,
        event.from_state,
        event.to_state,
        event.account_id,
        event.character_guid,
        event.session_requested,
        event.capacity_generation,
        event.evidence_digest,
    )
    if digest_value(payload) != event.payload_digest:
        raise ArtifactError("ledger event payload digest mismatch")
    if digest_value({"payload_digest": event.payload_digest, "payload": payload}) != event.event_key:
        raise ArtifactError("ledger event key digest mismatch")


def save_ledger(path: Path, ledger: DurableLedger) -> None:
    """Atomically persist one validated ledger artifact."""

    _require_digest(ledger.manifest_digest, "manifest_digest")
    for row in ledger.rows.values():
        _validate_row(row, ledger.manifest_digest)
    _validate_audit(ledger)
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ArtifactError(f"ledger parent must be an existing non-symlink directory: {parent}")
    content = _payload(ledger)
    document = dict(content)
    document["ledger_digest"] = digest_value(content)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_ledger(path: Path, manifest_digest: str) -> DurableLedger:
    """Load and validate a digest-bound ledger; invalid state never degrades open."""

    _require_digest(manifest_digest, "manifest_digest")
    if not path.is_file() or path.is_symlink():
        raise ArtifactError("ledger must be an existing regular non-symlink file")
    try:
        raw = load_json_object(path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    if set(raw) != _LEDGER_KEYS or raw["schema_version"] != SCHEMA:
        raise ArtifactError("ledger keyset or schema version is invalid")
    if raw["manifest_digest"] != manifest_digest:
        raise ArtifactError("ledger is bound to another immutable manifest")
    claimed = raw["ledger_digest"]
    content = dict(raw)
    del content["ledger_digest"]
    if not isinstance(claimed, str) or digest_value(content) != claimed:
        raise ArtifactError("ledger digest mismatch")
    revision = raw["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ArtifactError("ledger revision is invalid")
    rows_raw = raw["rows"]
    audit_raw = raw["audit"]
    if not isinstance(rows_raw, dict) or not isinstance(audit_raw, list):
        raise ArtifactError("ledger rows and audit must have object/list shapes")
    ledger = DurableLedger(manifest_digest=manifest_digest, revision=revision)
    for key, row_raw in rows_raw.items():
        if not isinstance(key, str):
            raise ArtifactError("ledger row keys must be strings")
        row = _row_from_raw(row_raw, manifest_digest)
        if row.identity.identity_key != key:
            raise ArtifactError("ledger row key does not match identity_key")
        ledger.rows[key] = row
    ledger.audit = [_event_from_raw(item) for item in audit_raw]
    _validate_audit(ledger)
    return ledger
