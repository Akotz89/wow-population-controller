"""Exact CPI-196 manifest gate; distribution data is never synthesized here."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType

from .canonical import digest_file, digest_value, load_json_object
from .config import RuntimeConfig
from .errors import ArtifactError
from .models import Identity

SCHEMA = "cpi-196-immutable-identity-manifest-v1"
_ROW_KEYS = set(Identity.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class IdentityManifest:
    manifest_digest: str
    decision_digest: str
    file_digest: str
    identities: tuple[Identity, ...]
    admission_holds: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    legacy_identity_keys: frozenset[str] = frozenset()
    validation_status: str = "ACCEPTED"
    eligibility_generation: int = 0
    eligibility_digest: str | None = None
    eligibility_artifacts: tuple[tuple[Path, str], ...] = ()


def _regular(path: Path, name: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise ArtifactError(f"{name} must be an existing regular non-symlink file")


def _load_exact_gate(path: Path, expected_digest: str) -> ModuleType:
    _regular(path, "identity gate")
    if digest_file(path) != expected_digest:
        raise ArtifactError("identity gate source digest mismatch")
    spec = importlib.util.spec_from_file_location("_cpi196_runtime_gate", path)
    if spec is None or spec.loader is None:
        raise ArtifactError("cannot construct the exact CPI-196 identity gate module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ArtifactError(f"cannot load the exact CPI-196 identity gate: {exc}") from exc
    if not callable(getattr(module, "validate_manifest", None)):
        raise ArtifactError("identity gate does not export validate_manifest")
    return module


def load_manifest(config: RuntimeConfig) -> IdentityManifest:
    _regular(config.manifest_path, "identity manifest")
    actual_file_digest = digest_file(config.manifest_path)
    if actual_file_digest != config.manifest_file_sha256:
        raise ArtifactError("identity manifest file digest mismatch")
    for path, name in (
        (config.decisions_path, "identity decisions"),
        (config.schedule_path, "wave schedule"),
        (config.template_catalogue_path, "template catalogue"),
        (config.appearance_catalogue_path, "appearance catalogue"),
    ):
        _regular(path, name)
    gate = _load_exact_gate(config.identity_gate_path, config.identity_gate_sha256)
    try:
        raw = load_json_object(config.manifest_path)
        validated = gate.validate_manifest(
            raw,
            gate.load_json(config.decisions_path),
            config.schedule_path,
            config.template_catalogue_path,
            config.appearance_catalogue_path,
        )
    except Exception as exc:
        raise ArtifactError(f"CPI-196 identity gate rejected the manifest: {exc}") from exc
    if (
        validated.get("schema_version") != SCHEMA
        or validated.get("status") not in {"ACCEPTED", "VALIDATED_WITH_HOLDS"}
        or validated.get("realm_id") != 1
        or validated.get("identity_count") != config.durable_target
        or not isinstance(validated.get("schedule_version"), str)
        or not validated["schedule_version"]
        or validated["schedule_version"] != getattr(gate, "SCHEDULE_VERSION", None)
        or validated.get("schedule_sha256") != getattr(gate, "SCHEDULE_SHA256", None)
        or validated.get("schedule_sha256") != digest_file(config.schedule_path)
        or type(getattr(gate, "ROSTER_TARGET", None)) is not int
        or config.durable_target != gate.ROSTER_TARGET
    ):
        raise ArtifactError("identity manifest release fields are not the governed realm-1 values")
    rows = validated.get("rows")
    if not isinstance(rows, list) or len(rows) != config.durable_target:
        raise ArtifactError("identity manifest cardinality differs from the configured durable target")
    identities: list[Identity] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != _ROW_KEYS:
            raise ArtifactError(f"validated identity row {index} has an unexpected shape")
        identities.append(Identity(**row))
    keys = {item.identity_key for item in identities}
    if len(keys) != len(identities):
        raise ArtifactError("validated identity rows contain duplicate keys")
    holds = validated.get("admission_holds", {})
    if validated["status"] == "VALIDATED_WITH_HOLDS" and "admission_holds" not in validated:
        raise ArtifactError("held manifest lacks explicit admission hold authority")
    if (
        not isinstance(holds, dict)
        or not set(holds) <= keys
        or any(not isinstance(reason, str) or not reason.strip() for reason in holds.values())
    ):
        raise ArtifactError("admission holds must name known identities with nonempty reasons")
    legacy_identity_keys = frozenset(
        proof["identity"]["identity_key"]
        for proof in validated.get("legacy_identity_proofs", [])
    )
    if not legacy_identity_keys <= keys:
        raise ArtifactError("legacy identity proof names an unknown identity")
    manifest_digest = validated.get("manifest_digest")
    decision_digest = validated.get("decision_digest")
    if not isinstance(manifest_digest, str) or not isinstance(decision_digest, str):
        raise ArtifactError("validated identity manifest digests are absent")
    eligibility_generation = 0
    eligibility_digest = None
    eligibility_artifacts: list[tuple[Path, str]] = []
    if (config.eligibility_path is None) != (config.eligibility_file_sha256 is None):
        raise ArtifactError("eligibility path and hash must be provided together")
    if config.eligibility_path is not None:
        _regular(config.eligibility_path, "admission eligibility")
        if digest_file(config.eligibility_path) != config.eligibility_file_sha256:
            raise ArtifactError("admission eligibility file digest mismatch")
        validate = getattr(gate, "validate_eligibility", None)
        if not callable(validate):
            raise ArtifactError("identity gate lacks eligibility validation")
        try:
            eligibility = validate(load_json_object(config.eligibility_path), validated)
        except Exception as exc:
            raise ArtifactError(f"identity gate rejected admission eligibility: {exc}") from exc
        payload = dict(eligibility)
        eligibility_digest = payload.pop("eligibility_digest", None)
        eligibility_generation = eligibility.get("generation")
        if (
            eligibility.get("manifest_digest") != manifest_digest
            or type(eligibility_generation) is not int or eligibility_generation <= 0
            or not isinstance(eligibility_digest, str) or digest_value(payload) != eligibility_digest
        ):
            raise ArtifactError("admission eligibility release binding is invalid")
        eligibility_artifacts.append((config.eligibility_path, config.eligibility_file_sha256))
        holds = dict(holds)
        verified_evidence: dict[Path, str] = {}
        for release in eligibility["releases"]:
            key = release["identity_key"]
            if key not in holds:
                raise ArtifactError("eligibility release is not an initially held identity")
            evidence = Path(release["evidence_path"])
            _regular(evidence, "eligibility evidence")
            if not evidence.is_absolute():
                raise ArtifactError("eligibility evidence must be absolute")
            evidence = evidence.resolve(strict=True)
            expected = release["evidence_sha256"]
            if evidence in verified_evidence:
                if verified_evidence[evidence] != expected:
                    raise ArtifactError("conflicting eligibility evidence hashes")
            else:
                if not evidence.is_absolute() or digest_file(evidence) != expected:
                    raise ArtifactError("eligibility evidence digest/path mismatch")
                verified_evidence[evidence] = expected
                eligibility_artifacts.append((evidence, expected))
            del holds[key]
    return IdentityManifest(
        manifest_digest=manifest_digest,
        decision_digest=decision_digest,
        file_digest=actual_file_digest,
        identities=tuple(identities),
        admission_holds=MappingProxyType(dict(holds)),
        legacy_identity_keys=legacy_identity_keys,
        validation_status=validated["status"],
        eligibility_generation=eligibility_generation,
        eligibility_digest=eligibility_digest,
        eligibility_artifacts=tuple(eligibility_artifacts),
    )
