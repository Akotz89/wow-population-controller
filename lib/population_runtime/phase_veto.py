"""Static current-source phase checks; never grants runtime or admission authority."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from .canonical import digest_file, digest_value

SCHEMA = "cpi-191-current-phase-veto-static-evidence-v2"
EXPECTED_MODULE_MANIFEST_SHA256 = "74068f9490dbd490498ec2cb0b9e621b2df83b0bec6de114b25ff4ee6bf9c185"
GATE = "modules/mod-progression-system/src/SharedPhaseGate.cpp"
STORAGE = "src/server/game/Entities/Player/PlayerStorage.cpp"
REQUIRED_CALLBACKS = (
    "OnPlayerGetProgressionLevelCeiling",
    "OnPlayerCanProgress",
    "OnPlayerCanLearnProgressionSpell",
    "OnPlayerCanLoadFromDB",
    "OnPlayerBeforeTeleport",
    "OnPlayerCanStoreProgressionItem",
    "OnPlayerCanPlaceAuctionBid",
    "OnPlayerCanCreateProgressionAuction",
    "OnPlayerCanJoinInBattlegroundQueue",
    "OnPlayerCanJoinInArenaQueue",
    "OnPlayerCanBattleFieldPort",
    "OnPlayerCanSellItem",
    "OnPlayerCanSendMail",
    "OnPlayerCanJoinLfg",
    "OnPlayerCanEnterMap",
    "OnPlayerCanSetTradeItem",
    "OnPlayerCanAcceptQuest",
    "OnPlayerCanProgressQuestObjective",
    "OnPlayerCanRewardQuest",
    "OnPlayerGetProgressionSkillCeiling",
    "OnPlayerCanSetProgressionSkill",
    "OnPlayerCanUseItem",
)


def evaluate_phase_veto(source_path: Path, provenance_path: Path,
                        approved_module_manifest_sha256: str = EXPECTED_MODULE_MANIFEST_SHA256) -> dict[str, Any]:
    """Verify the reviewed two-file phase delta and actual policy/creation checks."""
    failures: list[str] = []
    inputs: dict[str, str] = {}
    def read(path: Path) -> str:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing regular source input: {path}")
        inputs[str(path)] = digest_file(path)
        return path.read_text()
    gate = storage = ""
    try:
        manifest = json.loads(read(provenance_path))
        if inputs[str(provenance_path)] != approved_module_manifest_sha256:
            failures.append("phase manifest digest mismatch")
        if len(manifest) != 2 or {row["path"] for row in manifest} != {GATE, STORAGE}:
            raise ValueError("phase manifest must bind exactly gate and storage")
        source_root = source_path.parents[3]
        gate = read(source_path)
        storage = read(source_root / STORAGE)
        for row in manifest:
            if inputs[str(source_root / row["path"])] != row["after_sha256"]:
                failures.append(f"source digest mismatch: {row['path']}")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        failures.append(str(exc))
    for callback in REQUIRED_CALLBACKS:
        if callback not in gate:
            failures.append(f"missing phase callback: {callback}")
    for marker in (
        "ProgressionSystem::GetSharedPhaseSnapshot()",
        "snapshot.policyValid = PhasePolicy.valid",
        "snapshot.phaseLevelCeiling = PhasePolicy.valid ? PhasePolicy.levelCeiling : 0",
        'PhasePolicy.currentPhase == "1_19"',
        "PhasePolicy.levelCeiling == 19",
        "PhasePolicy.professionCeiling == 150",
        "PhasePolicy.valid ? std::min(ceiling, PhasePolicy.levelCeiling) : 0",
        "item->RequiredLevel > PhasePolicy.levelCeiling",
        "item->RequiredSkillRank > PhasePolicy.professionCeiling",
        'PlayerScript("shared_phase_gate_playerscript")',
    ):
        if marker not in gate:
            failures.append(f"missing phase policy: {marker}")
    try:
        equip = storage[storage.index("Item* Player::EquipNewItem("):storage.index("Item* Player::EquipItem(")]
        guard = equip.index("OnPlayerCanStoreProgressionItem")
        allocation = equip.index("Item::CreateItem")
        if not guard < equip.index("return nullptr", guard) < allocation:
            failures.append("direct equipment veto must precede allocation")
    except ValueError:
        failures.append("missing direct equipment veto")
    if "Python" in gate or "PopulationDirector" in gate:
        failures.append("phase authority must remain independent of Python/native director")
    payload = {"schema_version": SCHEMA, "status": "STATIC_BLOCKED" if failures else "STATIC_READY",
               "static_ready": not failures, "runtime_acceptance": False, "admission_authorized": False,
               "inputs": inputs, "phase": {"id": "1_19", "level_ceiling": 19,
               "source_of_authority": "mod-progression-system"},
               "required_callback_count": len(REQUIRED_CALLBACKS), "failures": failures}
    payload["evidence_digest"] = digest_value(payload)
    return payload
