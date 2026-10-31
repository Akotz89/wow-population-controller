"""Deterministic source-only gate for CPI-196 identity manifests.

This module performs no database, network, service, engine, or gameplay work.
It compiles owner-approved rows into one canonical artifact for a future
Python-engine population script to consume through separately accepted module
capabilities. Missing or provisional authority always fails closed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCHEDULE_SHA256 = "60b9a1a4ba8d7d526d318bc9ad9772baa4e8f9ba28fe5495ff288c2099408377"
SCHEDULE_FORMAT = "cpi-196-immutable-integer-schedule-v1"
SCHEDULE_VERSION = "cpi-196-rndbot-wave-policy-candidate-v1"
MANIFEST_SCHEMA = "cpi-196-immutable-identity-manifest-v1"
DECISIONS_SCHEMA = "cpi-196-identity-decisions-v1"
SOURCE_LOCK_SCHEMA = "cpi-196-source-lock-v1"
ROSTER_TARGET = 10_000
REALM_ID = 1
HEX64 = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ACCOUNT_NAME = re.compile(r"[A-Z0-9_]{1,17}")
CHARACTER_NAME = re.compile(r"[A-Za-z]{2,12}")

# Derived from exact core playercreateinfo.sql at the source-lock commit.
# Region is a creation-location group, not a later gameplay assignment.
RACE_RULES: dict[int, tuple[str, str, frozenset[int]]] = {
    1: ("alliance", "elwynn", frozenset({1, 2, 4, 5, 8, 9})),
    2: ("horde", "durotar", frozenset({1, 3, 4, 7, 9})),
    3: ("alliance", "dun_morogh", frozenset({1, 2, 3, 4, 5})),
    4: ("alliance", "teldrassil", frozenset({1, 3, 4, 5, 11})),
    5: ("horde", "tirisfal", frozenset({1, 4, 5, 8, 9})),
    6: ("horde", "mulgore", frozenset({1, 3, 7, 11})),
    7: ("alliance", "dun_morogh", frozenset({1, 4, 8, 9})),
    8: ("horde", "durotar", frozenset({1, 3, 4, 5, 7, 8})),
    10: ("horde", "eversong", frozenset({2, 3, 4, 5, 8, 9})),
    11: ("alliance", "azuremyst", frozenset({1, 2, 3, 5, 7, 8})),
}
NON_DK_CLASSES = frozenset({1, 2, 3, 4, 5, 7, 8, 9, 11})
ROLES = frozenset({"damage", "healer", "tank"})
APPEARANCE_FIELDS = (
    "race_id",
    "gender",
    "skin",
    "face",
    "hair_style",
    "hair_color",
    "facial_hair",
)
REQUIRED_DECISION_KEYS = {
    "schema_version",
    "status",
    "acceptance_id",
    "approved_by",
    "approved_at",
    "schedule_sha256",
    "identity_namespace",
    "account_capacity",
    "decision_modes",
    "accepted_template_catalogue_sha256",
    "accepted_template_ids",
    "approved_appearance_catalogue_sha256",
}
REQUIRED_MODES = {
    "composition": "explicit_rows",
    "account_partitioning": "explicit_rows",
    "account_naming": "explicit_rows",
    "character_naming": "explicit_rows",
    "appearance": "explicit_rows",
    "cross_wave_placement": "explicit_rows",
    "role_spec_intent": "explicit_rows",
}
ROW_KEYS = {
    "account_name",
    "character_name",
    "race_id",
    "class_id",
    "role_intent",
    "spec_template_id",
    "wave_owner_level",
    "wave_ordinal",
    "appearance",
}


class GateError(ValueError):
    """A CPI-196 gate invariant failed."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: object) -> str:
    return sha256_bytes(canonical_json(value))


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise GateError(f"{name} must be a list")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise GateError(f"{name} key mismatch: missing={missing}, unknown={unknown}")


def _integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise GateError(f"{name} is outside {minimum}..{maximum if maximum is not None else 'max'}")
    return value


def _text(value: Any, name: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise GateError(f"{name} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise GateError(f"{name} is not canonical")
    return value


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read canonical JSON from {path}: {exc}") from exc


def parse_schedule_bytes(content: bytes) -> dict[str, Any]:
    if sha256_bytes(content) != SCHEDULE_SHA256:
        raise GateError("schedule SHA-256 is not the governed CPI-196 schedule")
    if not content.endswith(b"\n") or b"\r" in content or content.startswith(b"\xef\xbb\xbf"):
        raise GateError("schedule must be canonical UTF-8 LF text")
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise GateError("schedule must be ASCII") from exc
    if len(lines) != 5:
        raise GateError("schedule must contain exactly five lines")
    expected_prefixes = (
        "format=",
        "realm_id=",
        "schedule_version=",
        "permanent_roster_target=",
        "waves=",
    )
    values: list[str] = []
    for line, prefix in zip(lines, expected_prefixes, strict=True):
        if not line.startswith(prefix) or not line[len(prefix) :]:
            raise GateError("schedule fields are missing or out of order")
        values.append(line[len(prefix) :])
    if values[0] != SCHEDULE_FORMAT or values[2] != SCHEDULE_VERSION:
        raise GateError("schedule format/version mismatch")
    if values[1] != str(REALM_ID) or values[3] != str(ROSTER_TARGET):
        raise GateError("schedule realm/target mismatch")
    if any(re.fullmatch(r"[1-9][0-9]*", item) is None for item in values[4].split(",")):
        raise GateError("schedule waves are not canonical positive decimals")
    waves = tuple(int(item) for item in values[4].split(","))
    if len(waves) != 80 or sum(waves) != ROSTER_TARGET:
        raise GateError("schedule must contain 80 waves summing to 10000")
    if any(left < right for left, right in zip(waves, waves[1:], strict=False)):
        raise GateError("schedule waves must be non-increasing")
    if waves[0] != 329 or waves[-1] != 30:
        raise GateError("schedule endpoint mismatch")
    return {
        "format": values[0],
        "realm_id": REALM_ID,
        "schedule_version": values[2],
        "permanent_roster_target": ROSTER_TARGET,
        "waves": list(waves),
        "schedule_sha256": SCHEDULE_SHA256,
    }


def parse_schedule(path: Path) -> dict[str, Any]:
    try:
        return parse_schedule_bytes(path.read_bytes())
    except OSError as exc:
        raise GateError(f"cannot read schedule {path}: {exc}") from exc


def validate_decisions(raw: Any) -> dict[str, Any]:
    decisions = _object(raw, "decisions")
    _exact_keys(decisions, REQUIRED_DECISION_KEYS, "decisions")
    if decisions["schema_version"] != DECISIONS_SCHEMA:
        raise GateError("decision schema mismatch")
    if decisions["status"] != "ACCEPTED":
        raise GateError("owner distribution decisions are unresolved; final manifest generation is forbidden")
    _text(decisions["acceptance_id"], "acceptance_id", TOKEN)
    _text(decisions["approved_by"], "approved_by")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", _text(decisions["approved_at"], "approved_at")) is None:
        raise GateError("approved_at must be canonical UTC seconds")
    if decisions["schedule_sha256"] != SCHEDULE_SHA256:
        raise GateError("decisions do not bind the governed schedule")
    _text(decisions["identity_namespace"], "identity_namespace", TOKEN)
    _integer(decisions["account_capacity"], "account_capacity", 1, 9)
    modes = _object(decisions["decision_modes"], "decision_modes")
    _exact_keys(modes, set(REQUIRED_MODES), "decision_modes")
    for name, required in REQUIRED_MODES.items():
        if modes[name] != required:
            raise GateError(f"decision mode {name} is unsupported by v1; expected {required}")
    for field in ("accepted_template_catalogue_sha256", "approved_appearance_catalogue_sha256"):
        _text(decisions[field], field, HEX64)
    template_ids = _list(decisions["accepted_template_ids"], "accepted_template_ids")
    if not template_ids or any(not isinstance(item, str) or TOKEN.fullmatch(item) is None for item in template_ids):
        raise GateError("accepted_template_ids must be a non-empty canonical list")
    if template_ids != sorted(set(template_ids)):
        raise GateError("accepted_template_ids must be sorted and unique")
    return decisions


def load_template_catalogue(path: Path, expected_sha256: str) -> dict[str, tuple[int, str]]:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise GateError("template catalogue SHA-256 mismatch")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateError("template catalogue is not UTF-8") from exc
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    expected_fields = [
        "template_id", "class_id", "class_name", "spec_no", "config_name",
        "role_intent", "link_levels", "random_weight", "random_slot",
    ]
    if not rows or list(rows[0]) != expected_fields:
        raise GateError("template catalogue header mismatch")
    result: dict[str, tuple[int, str]] = {}
    for row in rows:
        template_id = _text(row["template_id"], "template_id", TOKEN)
        class_id = int(row["class_id"])
        role = row["role_intent"]
        if class_id not in NON_DK_CLASSES or role not in ROLES or template_id in result:
            raise GateError("template catalogue contains an invalid/duplicate template")
        result[template_id] = (class_id, role)
    return result


def load_appearance_catalogue(path: Path, expected_sha256: str) -> set[tuple[int, ...]]:
    content = path.read_bytes()
    if sha256_bytes(content) != expected_sha256:
        raise GateError("appearance catalogue SHA-256 mismatch")
    raw = json.loads(content.decode("utf-8"))
    rows = _list(raw, "appearance catalogue")
    if not rows:
        raise GateError("appearance catalogue must contain accepted source-verified tuples")
    result: set[tuple[int, ...]] = set()
    for index, item in enumerate(rows):
        row = _object(item, f"appearance[{index}]")
        _exact_keys(row, set(APPEARANCE_FIELDS), f"appearance[{index}]")
        values = tuple(_integer(row[field], f"appearance[{index}].{field}", 0, 255) for field in APPEARANCE_FIELDS)
        if values[0] not in RACE_RULES or values[1] not in (0, 1):
            raise GateError("appearance catalogue contains an invalid race/gender")
        if values in result:
            raise GateError("appearance catalogue contains duplicate tuples")
        result.add(values)
    return result


def _normalize_rows(
    raw: Any,
    schedule: dict[str, Any],
    decisions: dict[str, Any],
    templates: dict[str, tuple[int, str]],
    appearances: set[tuple[int, ...]],
) -> list[dict[str, Any]]:
    rows = _list(raw, "identity rows")
    if len(rows) != ROSTER_TARGET:
        raise GateError("identity rows must contain exactly 10000 entries")
    accepted_templates = set(decisions["accepted_template_ids"])
    if not accepted_templates <= set(templates):
        raise GateError("accepted_template_ids contain unavailable templates")
    account_capacity = decisions["account_capacity"]
    account_classes: dict[str, set[int]] = defaultdict(set)
    account_slots: Counter[str] = Counter()
    names: set[str] = set()
    identity_keys: set[str] = set()
    wave_counts: Counter[int] = Counter()
    wave_ordinals: set[tuple[int, int]] = set()
    normalized: list[dict[str, Any]] = []
    expected_sort: list[tuple[int, int]] = []
    for index, item in enumerate(rows, start=1):
        row = _object(item, f"rows[{index - 1}]")
        _exact_keys(row, ROW_KEYS, f"rows[{index - 1}]")
        account_name = _text(row["account_name"], "account_name", ACCOUNT_NAME)
        character_name = _text(row["character_name"], "character_name", CHARACTER_NAME)
        name_key = character_name.casefold()
        if name_key in names:
            raise GateError(f"duplicate character name: {character_name}")
        names.add(name_key)
        race_id = _integer(row["race_id"], "race_id", 1, 11)
        class_id = _integer(row["class_id"], "class_id", 1, 11)
        if race_id not in RACE_RULES or class_id not in RACE_RULES[race_id][2]:
            raise GateError("illegal or excluded WotLK race/class pair")
        if class_id == 6:
            raise GateError("Death Knights cannot satisfy the binding level-1 identity rule")
        role = row["role_intent"]
        template_id = row["spec_template_id"]
        if role not in ROLES or template_id not in accepted_templates:
            raise GateError("row role/template is not accepted")
        if templates[template_id] != (class_id, role):
            raise GateError("row template does not match class/role intent")
        account_key_text = account_name.casefold()
        if class_id in account_classes[account_key_text]:
            raise GateError("one account cannot contain duplicate classes in the accepted non-DK packing model")
        if account_slots[account_key_text] >= account_capacity:
            raise GateError("account exceeds approved capacity")
        account_classes[account_key_text].add(class_id)
        account_slots[account_key_text] += 1
        wave = _integer(row["wave_owner_level"], "wave_owner_level", 1, 80)
        wave_ordinal = _integer(row["wave_ordinal"], "wave_ordinal", 1, schedule["waves"][wave - 1])
        wave_key = (wave, wave_ordinal)
        if wave_key in wave_ordinals:
            raise GateError("duplicate wave ordinal")
        wave_ordinals.add(wave_key)
        wave_counts[wave] += 1
        expected_sort.append(wave_key)
        appearance = _object(row["appearance"], "appearance")
        _exact_keys(appearance, set(APPEARANCE_FIELDS) - {"race_id"}, "appearance")
        appearance_values = (race_id,) + tuple(
            _integer(appearance[field], f"appearance.{field}", 0, 255)
            for field in APPEARANCE_FIELDS[1:]
        )
        if appearance_values[1] not in (0, 1) or appearance_values not in appearances:
            raise GateError("appearance tuple is not in the approved source-verified catalogue")
        faction, starter_region, _ = RACE_RULES[race_id]
        stable_input = {
            "identity_namespace": decisions["identity_namespace"],
            "realm_id": REALM_ID,
            "ordinal": index,
            "account_name": account_name,
            "character_name": character_name,
            "wave_owner_level": wave,
            "wave_ordinal": wave_ordinal,
        }
        identity_key = digest({"domain": "cpi-196-identity-v1", **stable_input})
        if identity_key in identity_keys:
            raise GateError("derived identity collision")
        identity_keys.add(identity_key)
        normalized.append(
            {
                "ordinal": index,
                "identity_key": identity_key,
                "guid_allocation_key": digest({"domain": "cpi-196-guid-allocation-v1", "identity_key": identity_key}),
                "account_key": digest({"domain": "cpi-196-account-v1", "realm_id": REALM_ID, "account_name": account_name}),
                "account_name": account_name,
                "account_slot": account_slots[account_key_text],
                "character_name": character_name,
                "faction": faction,
                "race_id": race_id,
                "class_id": class_id,
                "role_intent": role,
                "spec_template_id": template_id,
                "starter_region": starter_region,
                "wave_owner_level": wave,
                "wave_ordinal": wave_ordinal,
                "initial_level": 1,
                "appearance": {field: appearance[field] for field in APPEARANCE_FIELDS[1:]},
            }
        )
    if expected_sort != sorted(expected_sort):
        raise GateError("identity rows must be canonically ordered by wave then wave_ordinal")
    for wave, expected in enumerate(schedule["waves"], start=1):
        if wave_counts[wave] != expected:
            raise GateError(f"wave {wave} row count does not match governed schedule")
        if {ordinal for row_wave, ordinal in wave_ordinals if row_wave == wave} != set(range(1, expected + 1)):
            raise GateError(f"wave {wave} ordinals are not contiguous")
    return normalized


def generate_manifest(
    decisions_raw: Any,
    rows_raw: Any,
    schedule_path: Path,
    template_catalogue_path: Path,
    appearance_catalogue_path: Path,
) -> dict[str, Any]:
    schedule = parse_schedule(schedule_path)
    decisions = validate_decisions(decisions_raw)
    templates = load_template_catalogue(template_catalogue_path, decisions["accepted_template_catalogue_sha256"])
    appearances = load_appearance_catalogue(appearance_catalogue_path, decisions["approved_appearance_catalogue_sha256"])
    rows = _normalize_rows(rows_raw, schedule, decisions, templates, appearances)
    payload = {
        "schema_version": MANIFEST_SCHEMA,
        "status": "ACCEPTED",
        "realm_id": REALM_ID,
        "schedule_version": SCHEDULE_VERSION,
        "schedule_sha256": SCHEDULE_SHA256,
        "decision_digest": digest(decisions),
        "identity_count": ROSTER_TARGET,
        "canonical_order": "wave_owner_level,wave_ordinal",
        "runtime_binding_rule": "actual_account_id_and_character_guid_are_outputs_bound_once_to_keys",
        "rows": rows,
    }
    return {**payload, "manifest_digest": digest(payload)}


def validate_held_manifest(manifest, decisions, schedule_path, template_path, appearance_path):
    """Validate structural identity data without granting new template admission."""
    required = {'schema_version', 'status', 'identity_namespace', 'account_capacity',
                'schedule_sha256', 'available_template_catalogue_sha256',
                'candidate_appearance_catalogue_sha256', 'legacy_fixture_sha256'}
    _exact_keys(decisions, required, 'held decisions')
    if decisions['schema_version'] != 'cpi-196-held-decisions-v1' or decisions['status'] != 'STRUCTURAL_ONLY_NO_NEW_ADMISSION':
        raise GateError('held decision scope mismatch')
    _text(decisions['identity_namespace'], 'identity_namespace', TOKEN)
    _integer(decisions['account_capacity'], 'account_capacity', 1, 9)
    if decisions['schedule_sha256'] != SCHEDULE_SHA256:
        raise GateError('held schedule mismatch')
    templates = load_template_catalogue(template_path, decisions['available_template_catalogue_sha256'])
    appearances = load_appearance_catalogue(appearance_path, decisions['candidate_appearance_catalogue_sha256'])
    proofs = _list(manifest.get('legacy_identity_proofs'), 'legacy proofs')
    legacy = {}
    for proof in proofs:
        _exact_keys(proof, {'predecessor_fixture_sha256', 'predecessor_fixture_text', 'identity', 'account_id', 'character_guid'}, 'legacy proof')
        text = proof['predecessor_fixture_text']
        if not isinstance(text, str) or sha256_bytes(text.encode()) != proof['predecessor_fixture_sha256'] or proof['predecessor_fixture_sha256'] != decisions['legacy_fixture_sha256']:
            raise GateError('legacy predecessor bytes are not the exact reviewed fixture')
        fixture = json.loads(text)
        row = proof['identity']
        if fixture.get('status') != 'ONE_IDENTITY_RUNTIME_TEST_FIXTURE_NOT_CPI196_ACCEPTED' or fixture.get('identity') != row:
            raise GateError('legacy immutable identity differs from predecessor')
        _integer(proof['account_id'], 'legacy account', 1, 0xFFFFFFFF)
        _integer(proof['character_guid'], 'legacy guid', 1, 0xFFFFFFFF)
        key = _text(row['identity_key'], 'legacy identity key', HEX64)
        if key in legacy:
            raise GateError('duplicate legacy proof')
        legacy[key] = row
        pair = (row['class_id'], row['role_intent'])
        existing = templates.get(row['spec_template_id'])
        if existing is not None and existing != pair:
            raise GateError('legacy template conflicts with catalogue')
        templates[row['spec_template_id']] = pair
    rows = _list(manifest.get('rows'), 'held rows')
    source = [{name: row.get(name) for name in ROW_KEYS} for row in rows]
    normalization = {'accepted_template_ids': sorted(templates), 'account_capacity': decisions['account_capacity'], 'identity_namespace': decisions['identity_namespace']}
    # Reuse structural checks; this local list is availability, never admission authority.
    expected = _normalize_rows(source, parse_schedule(schedule_path), normalization, templates, appearances)
    used = set()
    for index, row in enumerate(rows):
        key = row.get('identity_key')
        if key in legacy:
            if row != legacy[key]:
                raise GateError('legacy row mutated')
            structural = dict(expected[index])
            for name in ('identity_key', 'account_key', 'guid_allocation_key'):
                structural[name] = row[name]
            if structural != row:
                raise GateError('legacy placement or structural field changed')
            expected[index] = row
            used.add(key)
    if used != set(legacy) or rows != expected:
        raise GateError('held rows do not regenerate with exact legacy proof')
    for field in ('identity_key', 'guid_allocation_key'):
        if len({row[field] for row in rows}) != len(rows):
            raise GateError('legacy substitution introduces key collision')
    accounts = {}
    for row in rows:
        prior = accounts.setdefault(row['account_name'], row['account_key'])
        if prior != row['account_key']:
            raise GateError('account name has inconsistent keys')
    holds = _object(manifest.get('admission_holds'), 'admission holds')
    if set(holds) != {row['identity_key'] for row in rows} - used:
        raise GateError('structural release must hold every new identity')
    for reason in holds.values():
        _text(reason, 'hold reason')
    payload = {'schema_version': MANIFEST_SCHEMA, 'status': 'VALIDATED_WITH_HOLDS',
               'realm_id': REALM_ID, 'schedule_version': SCHEDULE_VERSION,
               'schedule_sha256': SCHEDULE_SHA256, 'decision_digest': digest(decisions),
               'identity_count': ROSTER_TARGET, 'canonical_order': 'wave_owner_level,wave_ordinal',
               'runtime_binding_rule': 'actual_account_id_and_character_guid_are_outputs_bound_once_to_keys',
               'rows': expected, 'admission_holds': holds, 'legacy_identity_proofs': proofs}
    if manifest != {**payload, 'manifest_digest': digest(payload)}:
        raise GateError('held release metadata or digest mismatch')
    return manifest


def validate_manifest(
    manifest_raw: Any,
    decisions_raw: Any,
    schedule_path: Path,
    template_catalogue_path: Path,
    appearance_catalogue_path: Path,
) -> dict[str, Any]:
    manifest = _object(manifest_raw, "manifest")
    if manifest.get("status") == "VALIDATED_WITH_HOLDS":
        return validate_held_manifest(manifest, _object(decisions_raw, "held decisions"), schedule_path, template_catalogue_path, appearance_catalogue_path)
    if manifest.get("manifest_digest") is None or not isinstance(manifest["manifest_digest"], str):
        raise GateError("manifest_digest is missing")
    payload = dict(manifest)
    claimed = payload.pop("manifest_digest")
    if not HEX64.fullmatch(claimed) or digest(payload) != claimed:
        raise GateError("manifest digest mismatch")
    normalized_rows = _list(manifest.get("rows"), "manifest.rows")
    source_rows: list[dict[str, Any]] = []
    for index, item in enumerate(normalized_rows):
        row = _object(item, f"manifest.rows[{index}]")
        source_rows.append(
            {
                "account_name": row.get("account_name"),
                "character_name": row.get("character_name"),
                "race_id": row.get("race_id"),
                "class_id": row.get("class_id"),
                "role_intent": row.get("role_intent"),
                "spec_template_id": row.get("spec_template_id"),
                "wave_owner_level": row.get("wave_owner_level"),
                "wave_ordinal": row.get("wave_ordinal"),
                "appearance": row.get("appearance"),
            }
        )
    expected = generate_manifest(
        decisions_raw,
        source_rows,
        schedule_path,
        template_catalogue_path,
        appearance_catalogue_path,
    )
    if canonical_json(expected) != canonical_json(manifest):
        raise GateError("manifest does not byte-canonically regenerate from accepted authority")
    return manifest


def verify_source_lock(lock_raw: Any, root: Path) -> list[str]:
    lock = _object(lock_raw, "source lock")
    _exact_keys(lock, {"schema_version", "status", "entries"}, "source lock")
    if lock["schema_version"] != SOURCE_LOCK_SCHEMA or lock["status"] != "NO-BIND":
        raise GateError("source lock must remain NO-BIND until the final roster is accepted")
    verified: list[str] = []
    for index, item in enumerate(_list(lock["entries"], "source lock entries")):
        entry = _object(item, f"source lock entries[{index}]")
        kind = entry.get("kind")
        name = _text(entry.get("name"), "source entry name", TOKEN)
        expected = _text(entry.get("sha256"), "source entry sha256", HEX64)
        if kind == "file":
            path = (root / _text(entry.get("path"), "source entry path")).resolve()
            actual = sha256_bytes(path.read_bytes())
        elif kind == "git-object":
            repo = (root / _text(entry.get("repo"), "source entry repo")).resolve()
            commit = _text(entry.get("commit"), "source entry commit", re.compile(r"[0-9a-f]{40}"))
            object_path = _text(entry.get("object_path"), "source entry object path")
            result = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}:{object_path}"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                raise GateError(f"cannot read git object for {name}: {result.stderr.decode(errors='replace')}")
            actual = sha256_bytes(result.stdout)
        else:
            raise GateError(f"unsupported source-lock entry kind: {kind}")
        if actual != expected:
            raise GateError(f"source-lock digest mismatch for {name}")
        verified.append(name)
    return verified


def _write_exclusive(path: Path, content: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise GateError(f"refusing to overwrite immutable artifact: {path}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--decisions", type=Path, required=True)
    preflight.add_argument("--schedule", type=Path, required=True)
    generate = sub.add_parser("generate")
    for command in (generate,):
        command.add_argument("--decisions", type=Path, required=True)
        command.add_argument("--rows", type=Path, required=True)
        command.add_argument("--schedule", type=Path, required=True)
        command.add_argument("--templates", type=Path, required=True)
        command.add_argument("--appearances", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--decisions", type=Path, required=True)
    validate.add_argument("--schedule", type=Path, required=True)
    validate.add_argument("--templates", type=Path, required=True)
    validate.add_argument("--appearances", type=Path, required=True)
    sources = sub.add_parser("verify-sources")
    sources.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            parse_schedule(args.schedule)
            validate_decisions(load_json(args.decisions))
            print("BIND-ELIGIBLE: accepted decision authority is complete")
        elif args.command == "generate":
            manifest = generate_manifest(
                load_json(args.decisions), load_json(args.rows), args.schedule, args.templates, args.appearances
            )
            _write_exclusive(args.output, canonical_json(manifest) + b"\n")
            print(f"GENERATED: {args.output} manifest_digest={manifest['manifest_digest']}")
        elif args.command == "validate":
            manifest = validate_manifest(
                load_json(args.manifest), load_json(args.decisions), args.schedule, args.templates, args.appearances
            )
            print(f"VALID: manifest_digest={manifest['manifest_digest']}")
        else:
            names = verify_source_lock(load_json(args.lock), args.lock.parent.parent)
            print(f"SOURCE-LOCK VALID: {len(names)} entries: {', '.join(names)}")
    except (GateError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"NO-BIND: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def validate_eligibility(raw, manifest):
    if raw.get("schema_version")=="cpi-196-admission-eligibility-v2":return validate_ordinary_batch(raw,manifest)
    """Authorize one evidenced ordinary creation trial, not a template release."""
    required = {'schema_version','status','generation','manifest_digest','releases','eligibility_digest'}
    _exact_keys(raw,required,'eligibility')
    payload=dict(raw);claimed=payload.pop('eligibility_digest')
    if claimed != digest(payload) or raw['manifest_digest'] != manifest['manifest_digest']:
        raise GateError('eligibility digest or immutable manifest mismatch')
    if raw['schema_version'] != 'cpi-196-admission-eligibility-v1' or raw['status'] != 'REVIEWED_CONTROLLED_TRIAL':
        raise GateError('eligibility scope mismatch')
    _integer(raw['generation'],'eligibility generation',1,0xFFFFFFFF)
    releases=_list(raw['releases'],'eligibility releases')
    if len(releases)>1:
        raise GateError('this reviewed trial authorizes at most one identity')
    if not releases:
        return raw  # Explicit revocation retains every original admission hold.
    release=_object(releases[0],'trial release')
    _exact_keys(release,{'identity_key','immutable_row_sha256','trial_scope','evidence_sha256','evidence_path','template_acceptance'},'trial release')
    rows={row['identity_key']:row for row in manifest['rows']}
    key=release['identity_key'];row=rows.get(key)
    if row is None or key not in manifest['admission_holds'] or digest(row)!=release['immutable_row_sha256']:
        raise GateError('trial identity is not an exact initially held row')
    profiles = {'ordinary-level1-human-warrior-lifecycle': (1,1,0,12), 'ordinary-level1-r2-c1-lifecycle': (2,1,1,14), 'ordinary-level1-r1-c5-lifecycle': (1,5,0,12), 'ordinary-level1-r2-c7-lifecycle': (2,7,1,14)}
    profile = profiles.get(release['trial_scope'])
    if profile is None or release['template_acceptance'] is not False:
        raise GateError('trial does not accept talent templates')
    if (row['race_id'],row['class_id'],row['initial_level'])!=(profile[0],profile[1],1) or any(row['appearance'].values()):
        raise GateError('trial outside evidenced ordinary race/class/appearance')
    path=Path(_text(release['evidence_path'],'evidence path'))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise GateError('trial evidence must be absolute regular non-symlink file')
    if sha256_bytes(path.read_bytes()) != release['evidence_sha256']:
        raise GateError('trial evidence digest mismatch')
    evidence=load_json(path)
    if evidence.get('identity') != row or evidence.get('scope') != release['trial_scope'] or evidence.get('template_acceptance') is not False or evidence.get('status')!='SOURCE_AND_DBC_SUPPORTED_FOR_ORDINARY_TRIAL':
        raise GateError('trial evidence scope or immutable row mismatch')
    creation=evidence.get('playercreateinfo',{})
    if (creation.get('race'),creation.get('class'),creation.get('map'),creation.get('zone'))!=profile:
        raise GateError('trial creation location not verified')
    dbc=evidence.get('dbc',[])
    if {r.get('name') for r in dbc}!={'CharSections','CharHairGeosets','CharacterFacialHairStyles','ChrRaces','CharStartOutfit'}:
        raise GateError('trial appearance/outfit evidence incomplete')
    for entry in dbc:
        if not entry.get('matches') or not HEX64.fullmatch(entry.get('sha256','')):
            raise GateError('trial DBC evidence incomplete')
    return raw

"""Source-proven ordinary level-one tuples; no talent/gameplay acceptance claim."""
def index_source(corpus):
    dbc=corpus['dbc'];world=corpus['world']
    required={'CharSections','CharHairGeosets','CharacterFacialHairStyles','ChrRaces','CharStartOutfit','ChrClasses'}
    if not required<=set(dbc):raise ValueError('missing creation DBC')
    sections={(r[1],r[2],r[3],r[8],r[9]) for r in dbc['CharSections']['records'] if r[7]&1 and not r[7]&4}
    abilities={}
    for ability in dbc['SkillLineAbility']['records']:abilities.setdefault(ability[1],[]).append(ability)
    return dict(sections=sections,abilities=abilities,
        hair={(r[1],r[2],r[3]) for r in dbc['CharHairGeosets']['records']},
        facial={(r[0],r[1],r[2]) for r in dbc['CharacterFacialHairStyles']['records']},
        races={r[0]:r for r in dbc['ChrRaces']['records']},
        classes={r[0] for r in dbc['ChrClasses']['records']},
        outfits={tuple(r['race_class_gender_outfit'][:3]):r for r in dbc['CharStartOutfit']['records']},
        creation={(r['race'],r['class']):r for r in world['playercreateinfo']},
        items={r['entry']:r for r in world['item_template']},custom_items=world['playercreateinfo_item'],world=world,
        spells={r[0] for r in dbc['Spell']['records']},skills={r[0] for r in dbc['SkillLine']['records']},
        skill_race_class=dbc['SkillRaceClassInfo']['records'],skill_abilities=dbc['SkillLineAbility']['records'])

def valid_appearance(index,race,appearance):
    a=appearance;gender=a['gender'];skin=a['skin'];face=a['face'];hair=a['hair_style'];color=a['hair_color'];facial=a['facial_hair']
    if gender not in (0,1) or any(type(v) is not int or not 0<=v<=255 for v in a.values()):return False
    required=[(race,gender,0,0,skin),(race,gender,4,0,skin),(race,gender,1,face,skin),(race,gender,3,hair,color)]
    if not all(t in index['sections'] for t in required):return False
    if (race,gender,hair) not in index['hair'] or (race,gender,facial) not in index['facial']:return False
    # Exact ordinary native factory exception, not an invented missing-data fallback.
    excluded=race in (6,11) or (gender==1 and race not in (4,5))
    return facial==0 if excluded else (race,gender,2,facial,color) in index['sections']

def validate_ordinary_row(index,row):
    race,cls=row['race_id'],row['class_id'];gender=row['appearance']['gender']
    if row['initial_level']!=1 or cls==6 or race not in index['races'] or cls not in index['classes']:raise ValueError('unsupported ordinary level-one race/class')
    if (race,cls) not in index['creation'] or (race,cls,gender) not in index['outfits']:raise ValueError('missing creation location/outfit')
    if not valid_appearance(index,race,row['appearance']):raise ValueError('appearance tuple not supported by player non-DK DBC records')
    items=[i for i in index['outfits'][race,cls,gender]['items'] if i>0]
    for removal in index['custom_items']:
        if removal['race'] in (0,race) and removal['class'] in (0,cls) and removal['amount']<0 and removal['itemid'] in items:
            items.remove(removal['itemid'])
    items += [r['itemid'] for r in index['custom_items'] if r['race'] in (0,race) and r['class'] in (0,cls) and r['amount']>0]
    if not items:raise ValueError('empty starting outfit')
    for item in items:
        data=index['items'].get(item)
        if data is None or data['RequiredLevel']>1:raise ValueError('missing or inaccessible starting outfit item')
        for field,number in [('AllowableClass',cls),('AllowableRace',race)]:
            if data[field]!=-1 and not data[field]&(1<<(number-1)):raise ValueError('starting item race/class mismatch')
    world=index['world']
    race_stats=[r for r in world['player_race_stats'] if r['Race']==race]
    class_stats=[r for r in world['player_class_stats'] if r['Class']==cls and r['Level']==1]
    if len(race_stats)!=1 or len(class_stats)!=1 or class_stats[0]['BaseHP']<=0:raise ValueError('missing native starting stats')
    if any(race_stats[0][k]+class_stats[0][k]<=0 for k in ('Strength','Agility','Stamina','Intellect','Spirit')):raise ValueError('invalid starting attributes')
    if not any(r['Level']==1 and r['Experience']>0 for r in world['player_xp_for_level']):raise ValueError('missing ordinary XP progression')
    matches=lambda mask,number:mask==0 or bool(mask&(1<<(number-1)))
    skills=[]
    for skill in world['playercreateinfo_skills']:
        if not matches(skill['raceMask'],race) or not matches(skill['classMask'],cls):continue
        if skill['skill'] not in index['skills'] or not 0<=skill['rank']<16:raise ValueError('invalid starting skill')
        if any(r[1]==skill['skill'] and matches(r[2],race) and matches(r[3],cls) for r in index['skill_race_class']):skills.append(skill['skill'])
    if not skills:raise ValueError('no applicable native starting skills')
    for table,rfield,cfield,sfield in [('playercreateinfo_spell_custom','racemask','classmask','Spell'),('playercreateinfo_cast_spell','raceMask','classMask','spell')]:
        for spell in world[table]:
            if matches(spell[rfield],race) and matches(spell[cfield],cls) and spell[sfield] not in index['spells']:raise ValueError('missing starting spell')
    learned={a[2] for skill in skills for a in index['abilities'].get(skill,[])
             if matches(a[3],race) and matches(a[4],cls) and (a[9]==2 or a[9]==1 and a[7]<=1)}
    actions=[r for r in world['playercreateinfo_action'] if (r['race'],r['class'])==(race,cls)]
    if not actions:raise ValueError('no native starting actions')
    for action in actions:
        if not 0<=action['button']<144:raise ValueError('invalid starting action button')
        if action['type']==0 and (action['action'] not in index['spells'] or action['action'] not in learned):raise ValueError('missing starting action spell')
        if action['type']==128 and action['action'] not in index['items']:raise ValueError('missing starting action item')
    return True


def validate_ordinary_batch(raw,manifest):
    required={'schema_version','status','generation','manifest_digest','releases','eligibility_digest'}
    _exact_keys(raw,required,'ordinary eligibility')
    payload=dict(raw);claimed=payload.pop('eligibility_digest')
    if raw['schema_version']!='cpi-196-admission-eligibility-v2' or raw['status']!='SOURCE_VERIFIED_ORDINARY_LEVEL1_BATCH':raise GateError('ordinary eligibility scope')
    if claimed!=digest(payload) or raw['manifest_digest']!=manifest['manifest_digest']:raise GateError('ordinary eligibility binding')
    _integer(raw['generation'],'eligibility generation',1,0xFFFFFFFF)
    rows={r['identity_key']:r for r in manifest['rows']};seen=set();sources={};validated=set()
    releases=_list(raw['releases'],'ordinary releases')
    if len(releases)>len(rows):raise GateError('too many ordinary releases')
    for release in releases:
        _exact_keys(release,{'identity_key','immutable_row_sha256','trial_scope','evidence_path','evidence_sha256','template_acceptance'},'ordinary release')
        key=release['identity_key'];row=rows.get(key)
        if key in seen or row is None or key not in manifest['admission_holds'] or digest(row)!=release['immutable_row_sha256']:raise GateError('ordinary row binding')
        seen.add(key)
        if release['trial_scope']!='source-verified-ordinary-level1' or release['template_acceptance'] is not False:raise GateError('ordinary release does not accept talent templates')
        path=Path(_text(release['evidence_path'],'ordinary source path'));expected=release['evidence_sha256']
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or not HEX64.fullmatch(expected):raise GateError('ordinary evidence path/hash')
        path=path.resolve(strict=True)
        if path in sources:
            if sources[path][0]!=expected:raise GateError('conflicting source hashes')
            index=sources[path][1]
        else:
            content=path.read_bytes()
            if sha256_bytes(content)!=expected:raise GateError('ordinary source bytes mismatch')
            index=index_source(json.loads(content));sources[path]=(expected,index)
        profile=(path,row['race_id'],row['class_id'],row['initial_level'],tuple(sorted(row['appearance'].items())))
        if profile not in validated:
            try:validate_ordinary_row(index,row)
            except (KeyError,ValueError) as exc:raise GateError('ordinary source rejected: '+str(exc)) from exc
            validated.add(profile)
    return raw
