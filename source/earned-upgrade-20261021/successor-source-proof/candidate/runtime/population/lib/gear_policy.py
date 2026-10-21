"""CPI-211 effective planner adapter; native earned quests and exact copper-vendor upgrades.

Source/protocol candidate only. Scores approximate sockets/set/overflow/script
acceptance; a goal acknowledgement proves admission, never earned acquisition.
"""
from __future__ import annotations

import hashlib
import importlib.util
import importlib.machinery
import json
import math
import os
from pathlib import Path
import stat
import sys

from quest_policy import QuestPolicy, ProtocolError, _integer, _digest


def _json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ProtocolError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ProtocolError("nonfinite JSON")))


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ProtocolError("invalid expected SHA256")
    return value


def _file(path, expected, maximum):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ProtocolError("binding path must be absolute and regular")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ProtocolError("binding file size/type")
        data = stream.read(maximum + 1)
    if len(data) > maximum or hashlib.sha256(data).hexdigest() != _sha(expected):
        raise ProtocolError("binding file digest mismatch")
    return data


def _uint(value, name, maximum=2**32-1, minimum=0):
    value = _integer(value, name, minimum)
    if value > maximum:
        raise ProtocolError("overflow " + name)
    return value


class GearUnavailable(ValueError):
    """Expected unsupported gear request; ordinary native questing stays available."""


class GearInputs:
    """Load authenticated immutable inputs once, before the world callback."""
    def __init__(self, extension, documents, expected_accepted_sha256):
        self.extension = extension
        if hashlib.sha256(documents["accepted"]).hexdigest() != _sha(expected_accepted_sha256):
            raise ProtocolError("accepted manifest digest mismatch")
        accepted = _json(documents["accepted"])
        fields = {"schema_version", "status", "candidate_id", "generation", "graph_digest", "manifest_digest", "projection_digest"}
        if (not isinstance(accepted, dict) or set(accepted) != fields
                or accepted["schema_version"] != "cpi211-root-accepted-item-sources-v1"
                or accepted["status"] != "ROOT_ACCEPTED_EFFECTIVE_REALM_ITEM_SOURCES"):
            raise ProtocolError("unaccepted effective dataset")
        for field in ("candidate_id", "generation"):
            if not isinstance(accepted[field], str) or not accepted[field]:
                raise ProtocolError("missing accepted generation identity")
        for field in ("graph_digest", "manifest_digest", "projection_digest"):
            _sha(accepted[field])
        self.accepted = accepted
        self.handle = extension.load(documents["graph"], documents["manifest"], documents["projection"],
                                     documents["accepted"], expected_accepted_sha256)
        graph = _json(documents["graph"])
        self.reward_edges = {edge["edge_id"]: edge for edge in graph["edges"]
                             if edge["kind"] == "quest_reward"}
        self.vendor_edges = {edge["edge_id"]: edge for edge in graph["edges"] if edge["kind"] == "vendor_source"}
        self.profile = _json(documents["profile"])
        p = self.profile
        if set(p) != {"schema_version", "evidence", "phase", "snapshot_metadata", "actors", "score_scale", "targets", "alternative_quest_ids"} or p["schema_version"] != "cpi211-gear-runtime-profile-v1" or not p["evidence"]:
            raise ProtocolError("invalid gear profile")
        _uint(p["score_scale"], "score scale", 1000000, 1)
        self.sources = {}
        self.item_queries = {}
        projections = _json(extension.sources(self.handle))
        if not isinstance(projections, list):
            raise ProtocolError("projection must be array")
        self.item_ids = set()
        for projection in projections:
            skills, items, quests = set(), set(), set()
            item_id = _uint(projection["item_id"], "projection item", minimum=1)
            self.item_ids.add(item_id)
            items.add(item_id)
            for row in projection["sources"]:
                sid = row["source_id"]
                if sid in self.sources:
                    raise ProtocolError("duplicate source identity")
                self.sources[sid] = dict(row, item_id=item_id)
                if row["required_profession_skill_id"]:
                    skills.add(row["required_profession_skill_id"])
                for dep in row["craft_dependencies"]:
                    items.add(dep["item_id"])
                if row["source_ref"]["kind"] == "quest":
                    quests.add(row["source_ref"]["id"])
                group = row["quest_alternative_group"]
                if group is not None:
                    if group not in p["alternative_quest_ids"] or not p["alternative_quest_ids"][group]:
                        raise ProtocolError("missing complete alternative history metadata")
                    quests.update(p["alternative_quest_ids"][group])
            query = tuple(sorted(values) for values in (skills, items, quests))
            for values in query:
                for value in values:
                    _uint(value, "resource ID", minimum=1)
            self.item_queries[item_id] = query
        self.vendor_item_ids = tuple(sorted({row["item_id"] for row in self.sources.values()
            if row["source_kind"] == "vendor" and row["source_ref"]["kind"] == "vendor"
            and any(key.startswith(str(row["item_id"]) + ":") for key in p["targets"])}))
        if len(self.vendor_item_ids) > 64:
            raise ProtocolError("vendor authenticated item budget")
        # Bootstrap-only authenticated catalog; each callback asks for one extra quest.
        self.quest_ids = tuple(sorted({row["source_ref"]["id"] for row in self.sources.values()
            if row["source_kind"] == "quest_reward" and row["source_ref"]["kind"] == "quest"
            and any(key.startswith(str(row["item_id"]) + ":") for key in p["targets"])}))
        metadata = p["snapshot_metadata"]
        required = {"core_revision", "playerbots_revision", "content_revision", "content_graph_digest", "route_id", "route_revision", "catalogue_digest", "profile_id", "profile_version", "capability_fingerprint", "capabilities"}
        if set(metadata) != required or metadata["content_graph_digest"] != accepted["graph_digest"]:
            raise ProtocolError("profile snapshot provenance mismatch")
        for name in required - {"capabilities"}:
            if not isinstance(metadata[name], str) or not metadata[name]:
                raise ProtocolError("missing snapshot provenance")
        if not isinstance(metadata["capabilities"], dict) or any(type(v) is not bool for v in metadata["capabilities"].values()):
            raise ProtocolError("invalid bound capabilities")
        for key, row in p["targets"].items():
            if set(row) != {"expected_replacement_level", "profession_value", "evidence"} or not row["evidence"]:
                raise ProtocolError("target requires profile evidence")
            _uint(row["expected_replacement_level"], "replacement level", 80, 1)
            _uint(row["profession_value"], "profession value", 2**63-1)

    @classmethod
    def from_binding(cls, path, expected_sha256):
        binding = _json(_file(path, expected_sha256, 65536))
        if set(binding) != {"schema_version", "files"} or binding["schema_version"] != "cpi211-gear-runtime-binding-v1":
            raise ProtocolError("invalid gear runtime binding")
        descriptors = binding["files"]
        if set(descriptors) != {"graph", "manifest", "projection", "accepted", "profile", "extension"}:
            raise ProtocolError("incomplete gear runtime binding")
        documents = {}
        for name, row in descriptors.items():
            if set(row) != {"path", "sha256"}:
                raise ProtocolError("invalid input descriptor")
            documents[name] = _file(row["path"], row["sha256"], 128*1024*1024)
        # CPython loads this pinned file only at bootstrap. No callback imports/planner processes.
        path = descriptors["extension"]["path"]
        if "_pbguide_gear" in sys.modules:
            prior = sys.modules["_pbguide_gear"]
            if (getattr(prior, "__file__", None) != path
                    or getattr(prior, "_gear_source_sha256", None) != descriptors["extension"]["sha256"]
                    or not isinstance(getattr(prior, "__loader__", None), importlib.machinery.ExtensionFileLoader)):
                raise ProtocolError("extension already loaded from another path")
            extension = prior
        else:
            spec = importlib.util.spec_from_file_location("_pbguide_gear", path)
            if spec is None or not isinstance(spec.loader, importlib.machinery.ExtensionFileLoader):
                raise ProtocolError("compiled gear extension unavailable")
            extension = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(extension)
            extension._gear_source_sha256 = descriptors["extension"]["sha256"]
            sys.modules["_pbguide_gear"] = extension
        _file(path, descriptors["extension"]["sha256"], 128*1024*1024)
        inputs = cls(extension, documents, descriptors["accepted"]["sha256"])
        inputs.extension_sha256 = descriptors["extension"]["sha256"]
        inputs.binding_sha256 = expected_sha256
        return inputs


class GearPolicy(QuestPolicy):
    policy_version = "cpi211-effective-gear-quest-v1"

    def __init__(self, native, audit, *, inputs, **kwargs):
        for port in ("strategic_read_gear_snapshot", "strategic_read_gear_resources", "population_read_phase_snapshot"):
            if not callable(getattr(native, port, None)):
                raise ProtocolError("required gear native port unavailable: " + port)
        super().__init__(native, audit, **kwargs)
        self.inputs = inputs
        self._gear_audits = {}
        self._selected_reward = None
        self._gear_observed = 0
        self._quest_cursor = {}
        self._vendor_consumed = {}
        self._vendor_ambiguous = set()

    def _audit_recommendation(self, key, plan, gear, resource, phase):
        stable_plan = {name: value for name, value in plan.items()
                       if name not in {"plan_id", "created_at_seconds"}} if isinstance(plan, dict) else plan
        row = dict(policy_version=self.policy_version, identity_key=key, event="gear_recommendation",
                   plan=plan, score_mode=gear.get("score_mode"), requirement_scope=gear.get("requirement_scope"),
                   slot_scope=gear.get("slot_scope"), native_resources=resource, phase=phase, inventory_feasibility_evaluated=False,
                   acceptance="recommendation_only; unsupported executors and recursive acquisition remain unfinished")
        signature = hashlib.sha256(_encode(dict(row, plan=stable_plan)).encode("ascii")).hexdigest()
        if self._gear_audits.get(key) != signature:
            self.audit(row)
            self._gear_audits[key] = signature

    def _resource_query(self, gear, eligible):
        queries = [set(), set(), set()]
        # Only usable, positive, evidenced upgrade targets consume observation budgets.
        pending = {target["item_id"] for target in self._targets(gear, eligible)}
        visited = set()
        while pending:
            item_id = pending.pop()
            if item_id in visited:
                continue
            visited.add(item_id)
            query = self.inputs.item_queries.get(item_id, ((), (), ()))
            for result, values in zip(queries, query):
                result.update(values)
            pending.update(set(query[1]) - visited)
            for values, maximum in zip(queries, (64, 256, 256)):
                if len(values) > maximum:
                    raise GearUnavailable("resource_query_exceeds_native_budget")
        return tuple(sorted(values) for values in queries)

    def _copies(self, key, binding, quest_snapshot, eligible):
        gear = self.native.strategic_read_gear_snapshot(key, self._gear_observed)
        if (not isinstance(gear, dict) or gear.get("identity_key") != key
                or (gear.get("account_id"), gear.get("character_guid")) != tuple(binding)
                or gear.get("online") is not True or gear.get("control_allowed") is not True):
            raise ProtocolError("gear actor not admitted or controlled")
        vendor_ids = getattr(self.inputs, "vendor_item_ids", ())
        if vendor_ids:
            if not callable(getattr(self.native, "strategic_read_vendor_offers", None)) or not callable(getattr(self.native, "strategic_buy_vendor_item", None)):
                raise ProtocolError("required vendor native ports unavailable")
            gear["vendors"] = self.native.strategic_read_vendor_offers(key, list(vendor_ids))
        query = self._resource_query(gear, eligible)
        resource = self.native.strategic_read_gear_resources(key, *query)
        phase = self.native.population_read_phase_snapshot()
        if not isinstance(resource, dict) or set(resource) != {"identity_key", "account_id", "character_guid", "online", "control_allowed", "reason", "map_id", "zone_id", "level", "money_copper", "profession_skill_values", "item_counts", "rewarded_quest_ids"}:
            raise ProtocolError("invalid resource response fields")
        for row in (gear, resource):
            if (not isinstance(row, dict) or row.get("identity_key") != key
                    or (row.get("account_id"), row.get("character_guid")) != tuple(binding)
                    or row.get("online") is not True or row.get("control_allowed") is not True
                    or row.get("reason") != ""):
                raise ProtocolError("gear actor not admitted or controlled")
            _uint(row["account_id"], "account", minimum=1)
            _uint(row["character_guid"], "character GUID", 2**64-1, 1)
        for name in ("level", "map_id"):
            _uint(gear.get(name), name, 80 if name == "level" else 2**32-1, 1 if name == "level" else 0)
            if resource.get(name) != gear[name]:
                raise ProtocolError("gear/resource location or level drift")
        if quest_snapshot.get("level") != gear["level"]:
            raise ProtocolError("quest/gear level drift")
        actor = self.inputs.profile["actors"].get(key)
        if not isinstance(actor, dict) or set(actor) != {"account_id", "character_guid", "class_id", "spec_tab", "role", "evidence"} or not actor["evidence"]:
            raise ProtocolError("missing actor profile evidence")
        if any(gear.get(n) != actor[n] for n in ("account_id", "character_guid", "class_id", "spec_tab", "role")):
            raise ProtocolError("actor profile drift")
        expected = self.inputs.profile["phase"]
        if (set(expected) != {"phase_id", "policy_digest", "level_ceiling", "planner_phase"}
                or not isinstance(phase, dict) or phase.get("enabled") is not True or phase.get("policy_valid") is not True
                or any(phase.get(n) != expected[n] for n in ("phase_id", "policy_digest", "level_ceiling"))):
            raise ProtocolError("native phase drift")
        _uint(expected["planner_phase"], "planner phase", minimum=1)
        if gear["level"] > expected["level_ceiling"]:
            raise ProtocolError("level exceeds bound phase")
        _uint(resource.get("money_copper"), "money")
        _uint(resource.get("zone_id"), "zone")
        for name, requested in zip(("profession_skill_values", "item_counts"), query[:2]):
            rows = resource.get(name)
            if not isinstance(rows, dict) or set(rows) != set(requested):
                raise ProtocolError("resource copy incomplete")
            for k, v in rows.items():
                _uint(k, "resource ID", minimum=1)
                _uint(v, "resource value")
        rewarded = resource.get("rewarded_quest_ids")
        if not isinstance(rewarded, list) or len(set(rewarded)) != len(rewarded) or not set(rewarded) <= set(query[2]):
            raise ProtocolError("invalid rewarded history")
        for qid in rewarded:
            _uint(qid, "rewarded quest", minimum=1)
        return gear, resource, phase

    def _targets(self, gear, eligible, reasons=None):
        targets = {}
        equipment = gear.get("equipment")
        if not isinstance(equipment, list) or len(equipment) != 19:
            raise ProtocolError("incomplete equipment copy")
        slots = {row["slot"]: row["item"] for row in equipment}
        if set(slots) != set(range(19)):
            raise ProtocolError("invalid equipment slots")
        quests = gear.get("quests")
        if not isinstance(quests, list) or len(quests) > 26:
            raise ProtocolError("gear quest budget")
        vendors = gear.get("vendors", [])
        if not isinstance(vendors, list) or len(vendors) > 64:
            raise ProtocolError("vendor observation budget")
        vendor_rows = []
        for offer in vendors:
            if not isinstance(offer, dict) or set(offer) != {"request_id", "source_id", "vendor_guid", "vendor_entry", "vendor_slot", "price_copper", "stock", "money_copper", "reason", "reward"}:
                raise ProtocolError("invalid vendor offer")
            _sha(offer["request_id"])
            for name in ("vendor_guid", "vendor_entry", "vendor_slot", "price_copper", "stock", "money_copper"):
                _uint(offer[name], name, 2**64-1 if name == "vendor_guid" else 2**32-1,
                      1 if name in {"vendor_guid", "vendor_entry"} else 0)
            if offer["source_id"] not in getattr(self.inputs, "vendor_edges", {}):
                continue # Authenticated source index remains the authority, not the nearby catalog.
            if offer["reason"]:
                if reasons is not None: reasons.append("vendor:" + offer["reason"])
                continue
            reward = offer["reward"]
            if reward["item"]["entry"] not in getattr(self.inputs, "vendor_item_ids", ()):
                raise ProtocolError("vendor item outside authenticated query")
            vendor_rows.append(dict(quest_id=0, status="vendor", reason="", rewards=[reward]))
        seen = set()
        for quest in list(quests) + vendor_rows:
            vendor = quest["status"] == "vendor"
            qid = _uint(quest["quest_id"], "offered quest", minimum=0 if vendor else 1)
            if not vendor and qid in seen:
                raise ProtocolError("duplicate offered quest")
            seen.add(qid)
            if not vendor and (qid not in eligible or quest["status"] not in {"none", "incomplete", "complete"} or quest["reason"]):
                continue
            if quest["status"] == "none":
                entry = quest.get("giver_entry")
                if type(entry) is not int or not entry or abs(entry) > 2**31-1:
                    raise ProtocolError("invalid inactive quest giver")
                if quest.get("giver_map") != gear["map_id"]:
                    raise ProtocolError("inactive giver map mismatch")
                for name in ("giver_x", "giver_y", "giver_z"):
                    value = quest.get(name)
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ProtocolError("invalid inactive giver position")
            if len(quest["rewards"]) > 10:
                raise ProtocolError("reward budget")
            for reward in quest["rewards"]:
                if reward["reason"] or reward["requirement_result"] != 0 or reward["classification_allowed"] is not True:
                    continue
                item_id = _uint(reward["item"]["entry"], "reward item", minimum=1)
                if item_id not in self.inputs.item_ids:
                    raise ProtocolError("offered reward missing effective index")
                if len(reward["slots"]) > 19:
                    raise ProtocolError("slot budget")
                for comparison in reward["slots"]:
                    slot = _uint(comparison["slot"], "slot", 18)
                    if comparison["current"] != slots[slot]:
                        raise ProtocolError("slot/current equipment mismatch")
                    if comparison["candidate_score_valid"] is not True:
                        raise ProtocolError("invalid candidate score")
                    candidate = comparison["candidate_score"]
                    empty = comparison["current"]["entry"] == 0
                    if empty:
                        if comparison["current_score_valid"] is not False or comparison["current_score_reason"] != "empty_slot":
                            raise ProtocolError("invalid empty-slot score")
                        current = 0  # Explicit native empty_slot; no equipped instance has a score.
                    else:
                        if comparison["current_score_valid"] is not True:
                            raise ProtocolError("invalid current score")
                        current = comparison["current_score"]
                    for value in (candidate, current):
                        if type(value) not in (int, float) or not math.isfinite(value):
                            raise ProtocolError("nonfinite native score")
                    # Profile-bound fixed scaling; truncation toward zero is explicit approximation.
                    upgrade = (candidate - current) * self.inputs.profile["score_scale"]
                    if not math.isfinite(upgrade) or abs(upgrade) > 2**63-1:
                        raise ProtocolError("upgrade utility overflow")
                    if int(upgrade) <= 0:
                        if reasons is not None:
                            reasons.append(f"nonpositive_upgrade:{item_id}:{slot}")
                        continue
                    metadata = self.inputs.profile["targets"].get(f"{item_id}:{slot}")
                    if metadata is None:
                        if reasons is not None:
                            reasons.append(f"missing_replacement_profession_evidence:{item_id}:{slot}")
                        continue
                    target_id = f"target:item-{item_id}-slot-{slot}"
                    targets[target_id] = dict(stable_id=target_id, item_id=item_id, slot=slot,
                        playerbots_upgrade_score=int(upgrade), expected_replacement_level=metadata["expected_replacement_level"],
                        profession_value=metadata["profession_value"])
        return list(targets.values())

    def _select_goal(self, key, binding, snapshot, eligible, now):
        self._capture_event('selecting', key, binding)
        self._selected_reward = None
        eligible_ids = {q["quest_id"] for q in eligible}
        candidates = self.inputs.quest_ids
        previous = self.goals.get(key, 0)
        if previous in candidates and snapshot["current_goal_quest_id"] == previous:
            self._gear_observed = previous
        elif candidates:
            cursor = self._quest_cursor.get(key, 0) % len(candidates)
            self._gear_observed = candidates[cursor]
            self._quest_cursor[key] = (cursor + 1) % len(candidates)
        else:
            self._gear_observed = 0
        observed = self._gear_observed
        if observed and now >= self.retry.get((key, observed), 0):
            retry = (self.memory.failures.get(_digest(self._context(key, binding, observed, snapshot)), {})
                     if self.memory is not None else {})
            if self.memory is None or self._wall >= retry.get("retry_epoch", 0):
                eligible_ids.add(observed)
        try:
            copies = self._copies(key, binding, snapshot, eligible_ids)
        except GearUnavailable as error:
            return self._ordinary_fallback(key, binding, snapshot, eligible, now, str(error))
        gear, resource, phase = copies
        reasons = []
        targets = self._targets(gear, eligible_ids, reasons)
        if reasons:
            self.audit(dict(policy_version=self.policy_version, identity_key=key,
                event="gear_candidates_deferred", reasons=reasons, inventory_feasibility_evaluated=False))
        if not targets:
            return self._ordinary_fallback(key, binding, snapshot, eligible, now, "no_supported_upgrade_targets")
        p = self.inputs.profile
        request = dict(p["snapshot_metadata"], character_id=binding[1], level=gear["level"],
                       phase=p["phase"]["planner_phase"], map_id=gear["map_id"], zone_id=resource["zone_id"],
                       now_seconds=_uint(self.wall_clock(), "wall time", 4102444800), group_size=1,
                       population_admitted=True, schedule_active=True,
                       in_combat=False, on_taxi=False, in_battleground_or_arena=False,
                       logging_out=False, forced_travel=False, has_human_master=False)
        # Native control_allowed proves admitted solo/safety conditions; no readiness is inferred for sources.
        resources = dict(money_copper=resource["money_copper"],
            profession_skill_values=[dict(skill_id=k, value=v) for k, v in sorted(resource["profession_skill_values"].items())],
            consumed_quest_alternative_groups=sorted(group for group, ids in p["alternative_quest_ids"].items()
                if set(ids) & set(resource["rewarded_quest_ids"])),
            item_counts=[dict(item_id=k, count=v) for k, v in sorted(resource["item_counts"].items())],
            inventory_complete=False)
        plan = _json(self.inputs.extension.plan(self.inputs.handle, _encode(request), _encode(resources), _encode(targets)))
        self._audit_recommendation(key, plan, gear, resource, phase)
        if not isinstance(plan, dict) or plan.get("character_id") != binding[1] or plan.get("content_graph_digest") != self.inputs.accepted["graph_digest"] or plan.get("created_at_seconds") != request["now_seconds"] or plan.get("content_revision") != request["content_revision"] or plan.get("errors") != []:
            raise ProtocolError("invalid planner response")
        sid = plan.get("selected_source_id")
        if sid is None:
            if plan.get("selected_target_id") is not None or plan.get("selected_item_id") is not None:
                raise ProtocolError("partial selected source/target/item")
            return self._ordinary_fallback(key, binding, snapshot, eligible, now, "planner_selected_no_source")
        source = self.inputs.sources.get(sid)
        target = next((t for t in targets if t["stable_id"] == plan.get("selected_target_id")), None)
        if source is None or target is None or source["item_id"] != target["item_id"] or plan.get("selected_item_id") != target["item_id"]:
            raise ProtocolError("selected source/target/item mismatch")
        if not any(e.get("source_id") == sid and e.get("target_id") == target["stable_id"] and e.get("item_id") == target["item_id"] and e.get("eligible") is True and e.get("disposition") == "pursue" for e in plan.get("evaluations", [])):
            raise ProtocolError("selected evaluation missing")
        if source["source_kind"] == "vendor" and source["source_ref"]["kind"] == "vendor":
            self._purchase_vendor(key, binding, source, target, gear, resource)
            return None
        if source["source_kind"] != "quest_reward" or source["source_ref"]["kind"] != "quest":
            return self._ordinary_fallback(key, binding, snapshot, eligible, now, "unsupported_native_source:" + source["source_kind"])
        quest = source["source_ref"]["id"]
        if quest not in eligible_ids or quest in resource["rewarded_quest_ids"]:
            return self._ordinary_fallback(key, binding, snapshot, eligible, now, "selected_quest_inactive_or_rewarded")
        edge = self.inputs.reward_edges.get(sid)
        if not isinstance(edge, dict):
            raise ProtocolError("missing authenticated reward provenance")
        attrs = edge["attributes"]
        kind = attrs.get("reward_kind")
        index = _uint(attrs.get("native_reward_index"), "reward index", 5 if kind == "choice" else 3)
        item = target["item_id"]
        column = "RewardChoiceItemID" if kind == "choice" else "RewardItem"
        if (kind not in {"choice", "guaranteed"} or attrs.get("native_table") != "quest_template"
                or attrs.get("quest_id") != quest or edge["from"] != {"kind": "quest", "id": quest}
                or edge["to"] != {"kind": "item", "id": item}
                or attrs.get("sql_reward_column_suffix") != index + 1
                or attrs.get("native_item_column") != column + str(index + 1)
                or edge["provenance"] != {"source": "normalized.quest_reward",
                                          "natural_key": [quest, item, kind, index + 1]}):
            raise ProtocolError("invalid authenticated reward provenance")
        native_kind = "choice" if kind == "choice" else "fixed"
        offered = any(q["quest_id"] == quest and any(
            r["kind"] == native_kind and r["index"] == index and r["item"]["entry"] == item
            and not r["reason"] and r["requirement_result"] == 0 and r["classification_allowed"] is True
            and any(s["slot"] == target["slot"] for s in r["slots"]) for r in q["rewards"])
            for q in gear["quests"])
        if target not in self._targets(gear, {quest}):
            offered = False
        if not offered:
            raise ProtocolError("selected item not offered by selected quest")
        fresh = self._snapshot(key, binding, snapshot["observed_quest_id"])
        if fresh != snapshot or self._copies(key, binding, fresh, eligible_ids) != copies:
            raise ProtocolError("native state drift before quest handoff")
        self._selected_reward = (kind, index, item)
        # Retain only the final authenticated selection after both native drift checks.
        self._capture_event('selected', key, binding,
            dict(source_id=sid, quest_id=quest, kind=native_kind, index=index, item=item,
                 phase=phase, graph_digest=self.inputs.accepted['graph_digest'],
                 binding_sha256=self.inputs.binding_sha256, reward_edge=edge))
        return quest

    def _purchase_vendor(self, key, binding, source, target, gear, resource):
        if key in self._vendor_ambiguous:
            return # Only independent explicit reconciliation may release an ambiguous economic outcome.
        sid = source["source_id"]
        edge = self.inputs.vendor_edges.get(sid)
        if not isinstance(edge, dict):
            raise ProtocolError("missing authenticated vendor provenance")
        row = edge["attributes"].get("native_row")
        if (not isinstance(row, dict) or edge["from"] != source["source_ref"]
                or edge["to"] != {"kind": "item", "id": target["item_id"]}
                or row.get("entry") != source["source_ref"]["id"] or row.get("item") != target["item_id"]
                or row.get("ExtendedCost") != 0 or edge["provenance"] != {
                    "source": "world.npc_vendor", "natural_key": [row["entry"], row["item"], 0]}):
            raise ProtocolError("invalid authenticated vendor provenance")
        offers = [offer for offer in gear.get("vendors", []) if offer["source_id"] == sid
            and offer["vendor_entry"] == row["entry"] and offer["vendor_slot"] == row["slot"]
            and offer["reward"]["item"]["entry"] == target["item_id"] and not offer["reason"]
            and any(slot["slot"] == target["slot"] for slot in offer["reward"]["slots"])]
        if not offers:
            raise ProtocolError("selected item not offered by selected vendor")
        offer = sorted(offers, key=lambda value: (value["vendor_guid"], value["vendor_slot"]))[0]
        if resource["item_counts"].get(target["item_id"], 0) or offer["price_copper"] > resource["money_copper"]:
            return
        if self._vendor_consumed.get(key) == offer["request_id"]:
            return
        # Vendor requests never become quest IDs, active goals or renewal leases.
        self._vendor_consumed[key] = offer["request_id"]
        self._vendor_ambiguous.add(key) # A thrown call or malformed receipt also requires reconciliation.
        result = self.native.strategic_buy_vendor_item(key, offer["request_id"], sid, target["item_id"], target["slot"])
        fields = {"request_id", "status", "reason", "stage", "item", "price_copper", "money_before", "money_after", "item_before", "item_after", "item_guid", "observed_money_after", "observed_item_after", "observation_valid"}
        if (not isinstance(result, dict) or set(result) != fields or result["request_id"] != offer["request_id"]
                or result["status"] not in {"refused_before_effect", "committed", "indeterminate_after_effect"}):
            raise ProtocolError("invalid native vendor receipt; request remains consumed")
        for name in fields - {"request_id", "status", "reason", "stage", "observation_valid"}:
            _uint(result[name], "vendor receipt " + name, 2**64-1 if name == "item_guid" else 2**32-1)
        if type(result["observation_valid"]) is not bool or not isinstance(result["reason"], str) or not isinstance(result["stage"], str):
            raise ProtocolError("invalid native vendor receipt observation")
        if result["status"] == "committed":
            if (result["item"] != target["item_id"] or result["price_copper"] != offer["price_copper"]
                    or result["money_before"] - result["money_after"] != result["price_copper"]
                    or result["item_before"] != 0 or result["item_after"] != 1
                    or type(result["item_guid"]) is not int or result["item_guid"] <= 0):
                raise ProtocolError("native vendor committed receipt mismatch; request remains consumed")
        if result["status"] != "indeterminate_after_effect":
            self._vendor_ambiguous.discard(key)
        self.audit(dict(policy_version=self.policy_version, identity_key=key,
            event="vendor_purchase", source_id=sid, target=target, native_receipt=result,
            acceptance="native_receipt; durable_gameplay_acceptance_separate"))

    def _pending_goal(self, quest, snapshot):
        return (quest in self.inputs.quest_ids and snapshot["current_goal_quest_id"] == quest
                and snapshot.get("current_goal_acquiring") is True)

    def _set_goal(self, key, quest):
        if self._selected_reward is None:
            return super()._set_goal(key, quest)
        return self.native.strategic_set_quest_goal(key, quest, *self._selected_reward)

    def _ordinary_fallback(self, key, binding, snapshot, eligible, now, reason):
        quest = super()._select_goal(key, binding, snapshot, eligible, now)
        if snapshot.get("current_goal_acquiring") is True:
            self._forget_goal(key) # Stop renewing an unsupported acquire; native lease expires within5s.
        self._emit(key, "ordinary_fallback", quest or 0, "gear_deferred:" + reason)
        return quest
