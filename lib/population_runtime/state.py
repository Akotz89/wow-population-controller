"""Digest-bound, atomic durable policy cache and append-only audit records."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest_value, load_json_object
from .errors import ArtifactError
from .models import EarnedProgress, IdentityBinding

SCHEMA = "cpi-223-python-population-state-v1"
_STATE_KEYS = {
    "schema_version",
    "manifest_digest",
    "capability_manifest_digest",
    "capability_generation",
    "revision",
    "bindings",
    "admitted_identity_keys",
    "progress_cache",
    "progress_cursor",
    "last_admission_epoch",
    "activation_count",
    "last_selected_epoch",
    "desired_session_keys",
    "state_digest",
}


@dataclass(slots=True)
class RuntimeState:
    manifest_digest: str
    capability_manifest_digest: str
    capability_generation: int
    revision: int = 0
    bindings: dict[str, IdentityBinding] = field(default_factory=dict)
    admitted_identity_keys: set[str] = field(default_factory=set)
    progress_cache: dict[str, EarnedProgress] = field(default_factory=dict)
    progress_cursor: int = 0
    last_admission_epoch: int = 0
    activation_count: dict[str, int] = field(default_factory=dict)
    last_selected_epoch: dict[str, int] = field(default_factory=dict)
    desired_session_keys: set[str] = field(default_factory=set)
    pending_admission_key: str | None = None
    pending_admission_keys: list[str] = field(default_factory=list)


def _unsigned(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactError(f"{field_name} must be an unsigned integer")
    return value


def _payload(state: RuntimeState) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "manifest_digest": state.manifest_digest,
        "capability_manifest_digest": state.capability_manifest_digest,
        "capability_generation": state.capability_generation,
        "revision": state.revision,
        "bindings": {
            key: {name: getattr(binding, name) for name in IdentityBinding.__dataclass_fields__}
            for key, binding in sorted(state.bindings.items())
        },
        "admitted_identity_keys": sorted(state.admitted_identity_keys),
        "progress_cache": {
            key: {name: getattr(record, name) for name in EarnedProgress.__dataclass_fields__}
            for key, record in sorted(state.progress_cache.items())
        },
        "progress_cursor": state.progress_cursor,
        "last_admission_epoch": state.last_admission_epoch,
        "activation_count": dict(sorted(state.activation_count.items())),
        "last_selected_epoch": dict(sorted(state.last_selected_epoch.items())),
        "desired_session_keys": sorted(state.desired_session_keys),
        "pending_admission_key": state.pending_admission_key,
        "pending_admission_keys": list(state.pending_admission_keys),
    }


def new_state(
    manifest_digest: str, capability_manifest_digest: str, capability_generation: int
) -> RuntimeState:
    return RuntimeState(manifest_digest, capability_manifest_digest, capability_generation)


def load_state(
    path: Path,
    manifest_digest: str,
    capability_manifest_digest: str,
    capability_generation: int,
) -> RuntimeState:
    if not path.exists():
        return new_state(manifest_digest, capability_manifest_digest, capability_generation)
    if not path.is_file() or path.is_symlink():
        raise ArtifactError("runtime state must be a regular non-symlink file")
    try:
        raw = load_json_object(path)
    except ValueError as exc:
        raise ArtifactError(str(exc)) from exc
    if set(raw) not in (_STATE_KEYS, _STATE_KEYS | {"pending_admission_key"}, _STATE_KEYS | {"pending_admission_key", "pending_admission_keys"}) or raw["schema_version"] != SCHEMA:
        raise ArtifactError("runtime state keyset or schema version is invalid")
    claimed = raw["state_digest"]
    content = dict(raw)
    del content["state_digest"]
    if not isinstance(claimed, str) or digest_value(content) != claimed:
        raise ArtifactError("runtime state digest mismatch")
    if (
        raw["manifest_digest"] != manifest_digest
        or raw["capability_manifest_digest"] != capability_manifest_digest
        or raw["capability_generation"] != capability_generation
    ):
        raise ArtifactError("runtime state is bound to another immutable release generation")
    pending = raw.get("pending_admission_key")
    if pending is not None and (not isinstance(pending, str) or not pending):
        raise ArtifactError("runtime state pending admission is invalid")
    pending_keys = raw.get("pending_admission_keys", [])
    if (not isinstance(pending_keys, list) or
        any(not isinstance(key, str) or not key for key in pending_keys) or
        len(set(pending_keys)) != len(pending_keys)):
        raise ArtifactError("runtime state pending window is invalid")
    if pending is not None:
        if pending_keys:
            raise ArtifactError("ambiguous scalar and window pending admissions")
        pending_keys = [pending]
    bindings_raw = raw["bindings"]
    progress_raw = raw["progress_cache"]
    if not isinstance(bindings_raw, dict) or not isinstance(progress_raw, dict):
        raise ArtifactError("runtime state bridge cache must be an object")
    bindings: dict[str, IdentityBinding] = {}
    for key, row in bindings_raw.items():
        if not isinstance(key, str) or not isinstance(row, dict) or set(row) != set(IdentityBinding.__dataclass_fields__):
            raise ArtifactError("runtime state binding is invalid")
        binding = IdentityBinding(**row)
        if binding.identity_key != key or binding.account_id <= 0 or binding.character_guid <= 0:
            raise ArtifactError("runtime state binding readback is invalid")
        bindings[key] = binding
    progress: dict[str, EarnedProgress] = {}
    for key, row in progress_raw.items():
        if not isinstance(key, str) or not isinstance(row, dict) or set(row) != set(EarnedProgress.__dataclass_fields__):
            raise ArtifactError("runtime state earned-progress cache is invalid")
        record = EarnedProgress(**row)
        if record.identity_key != key or record.character_guid <= 0:
            raise ArtifactError("runtime state earned-progress readback is invalid")
        progress[key] = record

    def string_set(name: str) -> set[str]:
        value = raw[name]
        if not isinstance(value, list) or len(value) != len(set(value)) or not all(isinstance(item, str) for item in value):
            raise ArtifactError(f"runtime state {name} is invalid")
        return set(value)

    def string_int_map(name: str) -> dict[str, int]:
        value = raw[name]
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ArtifactError(f"runtime state {name} is invalid")
        return {key: _unsigned(item, name) for key, item in value.items()}

    return RuntimeState(
        manifest_digest=manifest_digest,
        capability_manifest_digest=capability_manifest_digest,
        capability_generation=capability_generation,
        revision=_unsigned(raw["revision"], "revision"),
        bindings=bindings,
        admitted_identity_keys=string_set("admitted_identity_keys"),
        progress_cache=progress,
        progress_cursor=_unsigned(raw["progress_cursor"], "progress_cursor"),
        last_admission_epoch=_unsigned(raw["last_admission_epoch"], "last_admission_epoch"),
        activation_count=string_int_map("activation_count"),
        last_selected_epoch=string_int_map("last_selected_epoch"),
        desired_session_keys=string_set("desired_session_keys"),
        pending_admission_key=None,
        pending_admission_keys=pending_keys,
    )


def _validate_parent(path: Path) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ArtifactError(f"artifact parent must be an existing non-symlink directory: {path.parent}")


def save_state(path: Path, state: RuntimeState) -> None:
    _validate_parent(path)
    content = _payload(state)
    document = dict(content)
    document["state_digest"] = digest_value(content)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_bytes(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
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


def append_audit(path: Path, record: dict[str, Any]) -> None:
    _validate_parent(path)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
