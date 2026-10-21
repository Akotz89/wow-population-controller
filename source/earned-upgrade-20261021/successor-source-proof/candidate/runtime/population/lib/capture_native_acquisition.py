#!/usr/bin/env python3
"""Copy typed loaded native observations; missing semantics never become defaults."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

DONOR = Path(__file__).resolve().with_name('producer')
# Minimal runtime dependency: this adjacent immutable decoder is shipped with the library.
from ingest_capture import CaptureError, decode, _uint, _digest


def accepted_offline_modules():
    """OFF TICK only: the accepted producer remains bound to its local WSL donor layout."""
    if str(DONOR) not in sys.path:
        sys.path.insert(0, str(DONOR))


# Verbatim primitives from effective_data.py, pinned above; no schema/compiler import at runtime.
def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as error:
        raise CaptureError('canonical JSON requires representable finite values') from error

def measurement(value):
    data = canonical(value)
    return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

def read_descriptor(descriptor, root):
    if not isinstance(descriptor, dict):
        raise CaptureError('file descriptor')
    path = descriptor.get('path', descriptor.get('file'))
    if not isinstance(path, str):
        raise CaptureError('file path')
    path = Path(path)
    if not path.is_absolute():
        if path.name != str(path) or path.name in {'.', '..'}:
            raise CaptureError('capture basename')
        path = root / path
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
        raise CaptureError('capture must be regular nonsymlink file')
    data = path.read_bytes()
    if type(descriptor.get('bytes')) is not int or descriptor['bytes'] < 0 or len(data) != descriptor['bytes'] or hashlib.sha256(data).hexdigest() != _digest(descriptor.get('sha256')):
        raise CaptureError('capture file custody')
    return data

def digest(value):
    return measurement(value)['sha256']

FAMILIES = {
    'quest_reward': ('quest', 'quest_reward'), 'creature_loot': ('creature', 'loot_source'),
    'gameobject_loot': ('gameobject', 'loot_source'), 'vendor': ('vendor', 'vendor_source'),
    'trainer_recipe': ('trainer', 'trainer_spell'), 'craft': ('recipe', 'recipe_product'),
    'pvp_reward': ('vendor', 'vendor_source'), 'dungeon_quest': ('quest', 'quest_reward'),
    'dungeon_drop': ('creature', 'loot_source'), 'disenchant': ('item', 'loot_source'),
    'prospecting': ('item', 'loot_source'), 'milling': ('item', 'loot_source'),
    'fishing': ('loot_template', 'loot_source'), 'skinning': ('creature', 'loot_source')}
FACT_FIELDS = {'key', 'family', 'source_entry', 'item_entry', 'minimum_count', 'maximum_count',
    'probability', 'frontier', 'stages', 'phase_mask', 'spawn_mask', 'event_ids', 'pool_ids',
    'condition', 'group_size', 'copper', 'travel', 'attempt', 'risk', 'contention', 'capabilities',
    'profession', 'alternative', 'reagents', 'raw_refs', 'variant', 'unresolved'}


ITEM_FIELDS = {'entry', 'class', 'subclass', 'inventory_type', 'item_level', 'required_level',
               'allowable_class', 'allowable_race', 'required_skill', 'required_skill_rank', 'name'}
QUEST_SIGNED = {'previous_quest_id','exclusive_group','signed_money','minimum_rep_value','maximum_rep_value'}
QUEST_FLAGS = {'repeatable','daily','weekly','monthly','seasonal','dungeon_finder'}
QUEST_LISTS = {'previous_quests','previous_chain_quests','exclusive_quests','conditions','giver_scripts','python_predicate_callbacks'}
PYTHON_PREDICATE_HOOKS = {'PLAYER_CAN_ACCEPT_QUEST','PLAYER_CAN_PROGRESS_QUEST_OBJECTIVE','PLAYER_CAN_REWARD_QUEST','PLAYER_ON_BEFORE_QUEST_COMPLETE'}
PHASE_TEMPLATE_POLICY_SCOPE='actual_shared_phase_gate_static_template_predicate;catalogue_phase_digest_and_generation;revalidate_at_native_action;not_actor_CanTake_truth'
PYTHON_PREDICATE_SCOPE = 'passive_global0_and_exact_entry_registry_observation;current_player_macro_dispatches_global0;revalidate_at_action'
QUEST_SOURCE_LISTS={'giver_source_missing','giver_entries','item_givers','quest_pool_ids','quest_event_ids','giver_spawns','required_items','required_source_items','required_targets'}
QUEST_SOURCE_FIELDS=QUEST_SOURCE_LISTS | {'source_rule_version','native_quest_level','native_suggested_players','native_low_level_hide_diff','giver_source_complete','phase_template_policy','quest_disabled'}
QUEST_COMPLETION_FIELDS={'rep_objective_faction','rep_objective_value','rep_objective_faction2','rep_objective_value2','breadcrumb_dependents','timed','exploration_or_event','cast_objective','required_player_kills'}
ITEM_SOURCE_FIELDS={'random_property','random_suffix'}
QUEST_FIELDS = {'entry','minimum_level','maximum_level','required_classes','allowable_races','flags','type','rewards',
    'required_skill','required_skill_rank','next_quest_id','next_quest_in_chain','minimum_rep_faction','maximum_rep_faction',
    'source_item','source_item_count','source_spell','time_allowed','event_id','breadcrumb_quest','reward_mail_template',
    'predicate_coverage'} | QUEST_SIGNED | QUEST_FLAGS | QUEST_LISTS

PHASE_FIELDS = {'enabled', 'policy_valid', 'phase_id', 'level_ceiling', 'policy_digest'}
DERIVED_FIELDS = {'key', 'source_entry', 'item_entry', 'minimum_count', 'maximum_count'}
# Every other field requires native predicate/measurement/policy evidence, even []/null/zero.
REQUIRED_SEMANTICS = FACT_FIELDS - DERIVED_FIELDS - {'raw_refs'}
FAMILY_GAPS = {
    'quest_reward': 'quest slot IDs/counts available; entitlement, script/condition/stage/rate and measured costs require evidence',
    'dungeon_quest': 'quest slots available; native dungeon classification and entitlement require evidence',
    'creature_loot': 'loaded loot groups/references/rates/conditions and spawn linkage not exported',
    'gameobject_loot': 'loaded loot/groups/references and gameobject/event/pool predicates not exported',
    'vendor': 'full loaded vendor lists, extended costs/stock and eligibility not exported',
    'trainer_recipe': 'loaded trainer spells/requirements and recipe joins not exported',
    'craft': 'postprocessed spells/products/reagent slots and skill/eligibility not exported',
    'pvp_reward': 'loaded reward vendors/extended costs and PvP entitlement not exported',
    'dungeon_drop': 'loaded dungeon/difficulty loot variants/groups/rates not exported',
    'disenchant': 'native disenchant selection/groups/rates not exported',
    'prospecting': 'native prospecting loot/requirements/rates not exported',
    'milling': 'native milling loot/requirements/rates not exported',
    'fishing': 'native fishing area/skill/loot/rates not exported',
    'skinning': 'native skinning loot/skill/spawn/rates not exported',
}


def shape(value, fields, reason):
    if not isinstance(value, dict) or set(value) != fields:
        raise CaptureError(reason)


def catalogue(value):
    shape(value, {'schema', 'valid', 'reason', 'observation_generation', 'phase', 'items', 'quests',
                  'item_count', 'quest_count', 'reward_count','player_hook_dispatch','player_hook_registry_initialized'}, 'native catalogue shape')
    if value['schema'] != 'core1-loaded-acquisition-catalog-v1':
        raise CaptureError('native catalogue schema')
    if value['valid'] is not True or value['reason'] != '':
        raise CaptureError('native catalogue invalid: ' + str(value['reason']))
    if type(value['observation_generation']) is not int or value['observation_generation'] < 1:
        raise CaptureError('native observation generation')
    phase = value['phase']
    shape(phase, PHASE_FIELDS, 'native phase fields')
    if phase['enabled'] is not True or phase['policy_valid'] is not True or phase['phase_id'] != '1_19' or phase['level_ceiling'] != 19:
        raise CaptureError('exact loaded current 1_19 phase required')
    native_phase_digest = phase['policy_digest']
    if not isinstance(native_phase_digest, str) or len(native_phase_digest) != 64 or any(c not in '0123456789abcdefABCDEF' for c in native_phase_digest):
        raise CaptureError('native phase policy digest')
    if type(value['player_hook_registry_initialized']) is not bool or not isinstance(value['player_hook_dispatch'],list):
        raise CaptureError('native player hook registry status')
    for dispatch in value['player_hook_dispatch']:
        shape(dispatch,{'hook_id','hook_name','script_names','scope'},'native enabled hook dispatch')
        _uint(dispatch['hook_id'])
        if not isinstance(dispatch['hook_name'],str) or not isinstance(dispatch['scope'],str) or not isinstance(dispatch['script_names'],list) or any(not isinstance(name,str) for name in dispatch['script_names']):
            raise CaptureError('native enabled hook names/scope')
    if not isinstance(value['items'], list) or not isinstance(value['quests'], list):
        raise CaptureError('native catalogue arrays')
    for name, fields in (('items', ITEM_FIELDS), ('quests', QUEST_FIELDS)):
        seen = set()
        for row in value[name]:
            actual_fields=fields-{'python_predicate_callbacks'} if name=='quests' and 'python_predicate_callbacks' not in row else fields
            optional=QUEST_SOURCE_FIELDS if name=='quests' else ITEM_SOURCE_FIELDS
            present=optional & set(row)
            if present and present!=optional:raise CaptureError('native incomplete source closure fields')
            completion=QUEST_COMPLETION_FIELDS & set(row) if name=='quests' else set()
            if completion and (completion!=QUEST_COMPLETION_FIELDS or not present):raise CaptureError('native incomplete completion closure fields')
            shape(row, actual_fields | present | completion, 'native ' + name + ' fields')
            if name=='items':
                for field in present:
                    if type(row[field]) is not int or not -(2**31)<=row[field]<2**31:raise CaptureError('native signed item variant field')
            if type(row['entry']) is not int or not row['entry'] or row['entry'] in seen:
                raise CaptureError('duplicate/invalid native ' + name + ' identity')
            seen.add(row['entry'])
            for field in actual_fields - {'rewards','name','predicate_coverage'} - (QUEST_SIGNED | QUEST_FLAGS | QUEST_LISTS if name=='quests' else set()):
                _uint(row[field])
            if name=='quests':
                for field in QUEST_SIGNED:
                    if type(row[field]) is not int or not -(2**31)<=row[field]<2**31:raise CaptureError('native signed quest field '+field)
                for field in QUEST_FLAGS:
                    if type(row[field]) is not bool:raise CaptureError('native quest flag '+field)
                if not isinstance(row['predicate_coverage'],str) or not row['predicate_coverage']:raise CaptureError('native predicate coverage')
                for field in QUEST_LISTS & set(row):
                    if not isinstance(row[field],list):raise CaptureError('native quest prerequisite array')
                    if field not in {'conditions','giver_scripts','python_predicate_callbacks'} and any(type(value) is not int or not (-(2**31) if field=='previous_quests' else 0)<=value<=(2**31-1 if field=='previous_quests' else 2**32-1) for value in row[field]):raise CaptureError('native prerequisite identity')
                if completion:
                    for field in ('rep_objective_faction','rep_objective_faction2','required_player_kills'):_uint(row[field])
                    for field in ('rep_objective_value','rep_objective_value2'):
                        if type(row[field]) is not int or not -(2**31)<=row[field]<2**31:raise CaptureError('native signed reputation objective')
                    for field in ('timed','exploration_or_event','cast_objective'):
                        if type(row[field]) is not bool:raise CaptureError('native completion flag')
                    if not isinstance(row['breadcrumb_dependents'],list):raise CaptureError('native reverse breadcrumb vector')
                    for entry in row['breadcrumb_dependents']:_uint(entry)
                if present:
                    if row['source_rule_version']!='core1-ordinary-quest-source-rules-v1' or type(row['native_quest_level']) is not int or not -(2**31)<=row['native_quest_level']<2**31 or type(row['giver_source_complete']) is not bool:raise CaptureError('native source rule/version')
                    if type(row['quest_disabled']) is not bool:raise CaptureError('native quest disabled flag')
                    policy=row['phase_template_policy']
                    shape(policy,{'known','configured','ready','template_allowed','scope'},'native static quest phase policy')
                    if any(type(policy[key]) is not bool for key in ('known','configured','ready','template_allowed')) or policy['scope']!=PHASE_TEMPLATE_POLICY_SCOPE:raise CaptureError('native static quest phase policy facts')
                    _uint(row['native_suggested_players'])
                    _uint(row['native_low_level_hide_diff'])
                    for field in QUEST_SOURCE_LISTS:
                        if not isinstance(row[field],list):raise CaptureError('native source closure vector')
                    if any(not isinstance(reason,str) or not reason for reason in row['giver_source_missing']):raise CaptureError('native giver missing reason')
                    if row['giver_source_complete'] and row['giver_source_missing']:raise CaptureError('native giver complete/missing disagreement')
                    for field in ('giver_entries','item_givers','quest_pool_ids','quest_event_ids'):
                        if any(type(v) is not int or not (-(2**31) if field=='giver_entries' else 0)<=v<=(2**31-1 if field=='giver_entries' else 2**32-1) for v in row[field]):raise CaptureError('native source relation identity')
                    for field in ('required_items','required_source_items','required_targets'):
                        seen_indexes=set()
                        for objective in row[field]:
                            identity_field='entry' if field=='required_targets' else 'item'
                            shape(objective,{'index',identity_field,'count'},'native objective slot fields')
                            _uint(objective['index']);_uint(objective['count'])
                            if type(objective[identity_field]) is not int or not (-(2**31) if identity_field=='entry' else 0)<=objective[identity_field]<=(2**31-1 if identity_field=='entry' else 2**32-1) or objective['index'] in seen_indexes:raise CaptureError('native objective identity/index')
                            seen_indexes.add(objective['index'])
                        if seen_indexes!=set(range(6 if field=='required_items' else 4)):raise CaptureError('native objective fixed-array census')
                    for spawn in row['giver_spawns']:
                        shape(spawn,{'entry','guid','map_id','phase_mask','spawn_mask','spawn_group_id','spawn_group_flags','script_id','multispawn','event_ids','pool_ids'},'native giver spawn fields')
                        if type(spawn['entry']) is not int or not -(2**31)<=spawn['entry']<2**31 or not spawn['entry'] or type(spawn['multispawn']) is not bool:raise CaptureError('native giver spawn identity')
                        for field in set(spawn)-{'entry','multispawn','event_ids','pool_ids'}:_uint(spawn[field])
                        for field in ('event_ids','pool_ids'):
                            if not isinstance(spawn[field],list) or any(type(v) is not int or not (-(2**31) if field=='event_ids' else 0)<=v<=(2**31-1 if field=='event_ids' else 2**32-1) for v in spawn[field]):raise CaptureError('native giver event/pool identities')
                seen_hooks=set()
                for callback in row.get('python_predicate_callbacks',[]):
                    shape(callback,{'hook_name','quest_id','native_dispatch_entry','registry_known','engine_enabled','reloading','has_global','has_global_or_quest_entry','scope'},'native passive Python callback fields')
                    hook=callback['hook_name']
                    if hook not in PYTHON_PREDICATE_HOOKS or hook in seen_hooks or callback['quest_id']!=row['entry'] or type(callback['quest_id']) is not int or type(callback['native_dispatch_entry']) is not int or callback['native_dispatch_entry']!=0 or callback['scope']!=PYTHON_PREDICATE_SCOPE:
                        raise CaptureError('native passive Python callback binding/scope')
                    seen_hooks.add(hook)
                    if any(type(callback[field]) is not bool for field in ('registry_known','engine_enabled','reloading','has_global','has_global_or_quest_entry')) or callback['has_global'] and not callback['has_global_or_quest_entry']:
                        raise CaptureError('native passive Python callback registry facts')
                for giver in row['giver_scripts']:
                    shape(giver,{'entry','relation','script_id','ai_name','registered_script_name','template_known','registry_resolved'},'native giver script')
                    if type(giver['entry']) is not int or not -(2**31)<=giver['entry']<2**31:raise CaptureError('native giver entry')
                    _uint(giver['relation']);_uint(giver['script_id'])
                    if any(type(giver[f]) is not bool for f in ('template_known','registry_resolved')) or any(not isinstance(giver[f],str) for f in ('ai_name','registered_script_name')):raise CaptureError('native giver script registry facts')
                for condition in row['conditions']:
                    shape(condition,{'source_type','source_group','source_entry','source_id','else_group','condition_type','value1','value2','value3','reference_id','script_id','target','negative'},'native condition row')
                    if type(condition['negative']) is not bool:raise CaptureError('native negative condition flag')
                    for field,condition_value in condition.items():
                        if field=='source_entry':
                            if type(condition_value) is not int or not -(2**31)<=condition_value<2**31:raise CaptureError('native condition source entry')
                        elif field!='negative':_uint(condition_value)
            if name == 'items' and not isinstance(row['name'], str):
                raise CaptureError('native item name')
    item_ids = {row['entry'] for row in value['items']}
    slots = []
    for quest in value['quests']:
        if not isinstance(quest['rewards'], list):
            raise CaptureError('native quest reward array')
        seen = set()
        for reward in quest['rewards']:
            shape(reward, {'kind', 'index', 'item', 'count'}, 'native reward fields')
            if reward['kind'] not in {'fixed', 'choice'} or type(reward['index']) is not int or not 0 <= reward['index'] < (4 if reward['kind'] == 'fixed' else 6):
                raise CaptureError('native reward slot')
            identity = (reward['kind'], reward['index'])
            if identity in seen:
                raise CaptureError('duplicate native reward slot')
            seen.add(identity)
            if type(reward['item']) is not int or reward['item'] not in item_ids or type(reward['count']) is not int or not 0 < reward['count'] <= 4294967295:
                raise CaptureError('native reward missing item/count')
            slots.append(dict(key=f"quest:{quest['entry']}:{reward['kind']}:{reward['index']}",
                source_entry=quest['entry'], item_entry=reward['item'], minimum_count=reward['count'],
                maximum_count=reward['count'], native_reward=reward, native_quest=quest))
    for name, actual in (('item_count', len(value['items'])), ('quest_count', len(value['quests'])), ('reward_count', len(slots))):
        if type(value[name]) is not int or value[name] != actual:
            raise CaptureError('native complete count mismatch: ' + name)
    return slots


def inspect(value):
    slots = catalogue(value)
    return dict(schema='core1-native-capture-diagnostics-v1',
        catalogue_digest=digest(value), observation_generation=value['observation_generation'],
        phase=value['phase'], effective_item_count=value['item_count'], quest_count=value['quest_count'],
        reward_slot_count=len(slots), quest_slots=slots,
        missing_per_slot=sorted(REQUIRED_SEMANTICS | {'raw_refs'}),
        family_dispositions=FAMILY_GAPS, source_to_loaded_identity='unverified',
        full37_complete=False, sealed_generation_ready=False,
        missing=['same-generation raw/config/SQL/DBC/locales/source custody',
                 'independently evidenced native predicates, family closure and measurements',
                 'S02-S37 historical beforeimages and owner stage authority'])


def prepare_current_observation(catalog, observed_quest_id):
    """Off-tick catalogue validation/index preparation; no full-catalogue work in the actor callback."""
    selected = [slot for slot in catalogue(catalog) if slot['source_entry'] == observed_quest_id]
    if not selected:
        raise CaptureError('collector observed quest missing native catalogue slots')
    item_ids = {slot['item_entry'] for slot in selected}
    return decode(canonical(dict(schema='core1-current-observation-context-v1', catalogue_digest=digest(catalog),
        observation_generation=catalog['observation_generation'], phase=catalog['phase'], slots=selected,
        items=[item for item in catalog['items'] if item['entry'] in item_ids],
        player_hook_dispatch=catalog['player_hook_dispatch'],player_hook_registry_initialized=catalog['player_hook_registry_initialized'])))


def _observation_context(context):
    shape(context, {'schema', 'catalogue_digest', 'observation_generation', 'phase', 'slots', 'items','player_hook_dispatch','player_hook_registry_initialized'}, 'prepared current observation context')
    if context['schema'] != 'core1-current-observation-context-v1':
        raise CaptureError('prepared current observation context schema')
    _digest(context['catalogue_digest'])
    return context['slots']


def collect_actor_observations(population, catalog, identity_key, observed_quest_id):
    """One read-only call inside the EXISTING admitted population tick; native gates remain authoritative."""
    slots = _observation_context(catalog)
    if not isinstance(identity_key, str) or len(identity_key) != 64 or any(c not in '0123456789abcdef' for c in identity_key):
        raise CaptureError('collector admitted identity format')
    if type(observed_quest_id) is not int or observed_quest_id < 1:
        raise CaptureError('collector observed quest identity')
    selected = [slot for slot in slots if slot['source_entry'] == observed_quest_id]
    if not selected:
        raise CaptureError('collector observed quest missing native catalogue slots')
    # These are the original actor Provider() bindings; public catalogue access cannot replace them.
    gear = population.strategic_read_gear_snapshot(identity_key, observed_quest_id)
    if not isinstance(gear, dict) or gear.get('identity_key') != identity_key or gear.get('online') is not True or gear.get('control_allowed') is not True or gear.get('reason') != '':
        raise CaptureError('collector actor not admitted/online/controlled')
    if any(type(gear.get(field)) is not int or gear[field] < 1 for field in ('account_id', 'character_guid')):
        raise CaptureError('collector durable actor binding')
    item_ids = sorted({slot['item_entry'] for slot in selected})
    item_rows = {row['entry']: row for row in catalog['items']}
    skill_ids = sorted({item_rows[item]['required_skill'] for item in item_ids if item_rows[item]['required_skill']})
    resource = population.strategic_read_gear_resources(identity_key, skill_ids, item_ids, [observed_quest_id])
    phase = population.population_read_phase_snapshot()
    if not isinstance(resource, dict) or any(resource.get(field) != gear[field] for field in ('identity_key', 'account_id', 'character_guid', 'map_id', 'level')) or resource.get('online') is not True or resource.get('control_allowed') is not True or resource.get('reason') != '':
        raise CaptureError('collector actor/resource identity or location drift')
    if phase != catalog['phase']:
        raise CaptureError('collector native phase drift')
    for field, requested in (('item_counts', item_ids), ('profession_skill_values', skill_ids)):
        rows = resource.get(field)
        if not isinstance(rows, dict) or set(rows) != set(requested):
            raise CaptureError('collector requested resource copy incomplete')
        for count in rows.values():
            _uint(count)
    quests = [row for row in gear.get('quests', []) if row.get('quest_id') == observed_quest_id]
    if len(quests) != 1 or quests[0].get('reason') != '':
        raise CaptureError('collector observed native quest unavailable')
    rewards = {(row['kind'], row['index']): row for row in quests[0].get('rewards', [])}
    if len(rewards) != len(quests[0].get('rewards', [])) or set(rewards) != {(slot['native_reward']['kind'], slot['native_reward']['index']) for slot in selected}:
        raise CaptureError('collector native reward slot coverage drift')
    for slot in selected:
        reward = rewards[(slot['native_reward']['kind'], slot['native_reward']['index'])]
        if reward.get('item', {}).get('entry') != slot['item_entry'] or reward.get('item', {}).get('count') != slot['minimum_count']:
            raise CaptureError('collector native reward identity/yield drift')
    return dict(gear=gear, resources=resource, phase=phase,
        scope='existing admitted actor; requested resources only; script/final equip acceptance not inferred')


def capture_current_observations(population, catalog, identity_key, observed_quest_id, snapshot_id, source_revision):
    """Timed callback seam: native value copies only, no file IO, receipt loading or request assembly."""
    _digest(source_revision)
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise CaptureError('collector observation snapshot identity')
    actor = collect_actor_observations(population, catalog, identity_key, observed_quest_id)
    return dict(snapshot_id=snapshot_id, kind='native_execution', source_revision=source_revision,
        data=dict(catalogue_digest=catalog['catalogue_digest'], observation_generation=catalog['observation_generation'],
                  identity_key=identity_key, observed_quest_id=observed_quest_id, actor=actor))


class QuestAcquisitionMeasurement:
    """One exact selected slot, observed through unchanged admitted resource/phase getters.

    This records an observation window, not item-origin causality, travel time, risk or contention.
    Lifecycle callsites and an accepted normalization policy remain separately owned prerequisites.
    """
    def __init__(self, population, catalog, identity_key, slot_key, snapshot_id, source_revision, clock=None):
        import time
        slots = [slot for slot in _observation_context(catalog) if slot['key'] == slot_key]
        if len(slots) != 1:
            raise CaptureError('measurement exact original native slot required')
        import copy
        self.slot = copy.deepcopy(slots[0]); self.population = population
        _digest(identity_key); _digest(source_revision)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise CaptureError('measurement source snapshot identity')
        self.identity = identity_key; self.snapshot = snapshot_id; self.revision = source_revision
        self.phase = copy.deepcopy(catalog['phase']); self.generation = catalog['observation_generation']
        self.clock = time.monotonic_ns if clock is None else clock
        self.finished = False
        self.before = self._read()
        if self.slot['source_entry'] in self.before['rewarded_quest_ids']:
            raise CaptureError('measurement quest already rewarded before start')
        self.started = self.clock()
        if type(self.started) is not int or not 0 <= self.started <= 18446744073709551615:
            raise CaptureError('measurement monotonic timestamp range')

    def _read(self):
        value = self.population.strategic_read_gear_resources(self.identity, [], [self.slot['item_entry']], [self.slot['source_entry']])
        phase = self.population.population_read_phase_snapshot()
        if not isinstance(value, dict) or value.get('identity_key') != self.identity or value.get('online') is not True or value.get('control_allowed') is not True or value.get('reason') != '':
            raise CaptureError('measurement actor not admitted/online/controlled')
        if any(type(value.get(field)) is not int or value[field] < 1 for field in ('account_id', 'character_guid')):
            raise CaptureError('measurement durable identity binding')
        counts = value.get('item_counts'); rewarded = value.get('rewarded_quest_ids')
        if not isinstance(counts, dict) or set(counts) != {self.slot['item_entry']} or not isinstance(rewarded, list) or any(type(quest) is not int or quest != self.slot['source_entry'] for quest in rewarded) or len(rewarded) > 1:
            raise CaptureError('measurement requested resource completeness')
        _uint(counts[self.slot['item_entry']])
        if phase != self.phase:
            raise CaptureError('measurement native phase drift')
        import copy
        return copy.deepcopy(value)

    def finish(self):
        if self.finished:
            raise CaptureError('measurement already finalized')
        after = self._read(); ended = self.clock()
        if type(ended) is not int or not self.started < ended <= 18446744073709551615:
            raise CaptureError('measurement monotonic time/overflow')
        if any(after[field] != self.before[field] for field in ('identity_key', 'account_id', 'character_guid')):
            raise CaptureError('measurement actor identity drift')
        if self.slot['source_entry'] not in after['rewarded_quest_ids']:
            raise CaptureError('measurement native rewarded history absent')
        increase = after['item_counts'][self.slot['item_entry']] - self.before['item_counts'][self.slot['item_entry']]
        if increase < self.slot['minimum_count']:
            raise CaptureError('measurement native item count increase absent/insufficient')
        elapsed = ended - self.started
        data = dict(slots={self.slot['key']: {}}, native_slot=self.slot, phase=self.phase,
            observation_generation=self.generation, started_monotonic_ns=self.started, ended_monotonic_ns=ended,
            observed_duration_ns=elapsed, native_resources_before=self.before, native_resources_after=after,
            scope='native rewarded-history plus count increase over an observation window; not causal item-origin/earned/equip proof',
            missing=['attempt normalization policy', 'travel start/arrival witness', 'risk sample definition/evidence', 'contention sample definition/evidence'])
        self.finished = True
        return dict(snapshot_id=self.snapshot, kind='measurement', source_revision=self.revision, data=data)



class FiniteAcquisitionCapture:
    """One opt-in actor/slot lifecycle. Preparation and drain are off tick; events only copy native values.

    Selection is an authenticated policy recommendation, never per-source semantic or reward causality authority.
    No total row/time limit, daemon, disk queue or implicit retry is introduced.
    """
    def __init__(self, population, context, identity_key, binding, slot_key, source_id,
                 snapshot_id, source_revision, *, policy_generation, in_tick, clock=None):
        import copy
        if in_tick():
            raise CaptureError('capture arm must be off tick')
        slots = [slot for slot in _observation_context(context) if slot['key'] == slot_key]
        if len(slots) != 1 or not isinstance(source_id, str) or not source_id:
            raise CaptureError('capture exact source/slot required')
        _digest(identity_key); _digest(source_revision); _digest(policy_generation)
        if not isinstance(snapshot_id, str) or not snapshot_id or len(binding) != 2 or any(type(v) is not int or v < 1 for v in binding):
            raise CaptureError('capture snapshot/durable actor binding')
        self.population = population; self.context = copy.deepcopy(context)
        self.identity = identity_key; self.binding = tuple(binding); self.slot = copy.deepcopy(slots[0])
        self.source = source_id; self.snapshot = snapshot_id; self.revision = source_revision
        self.policy_generation = policy_generation
        self.in_tick = in_tick; self.clock = clock
        self.state = 'armed'; self.selection = None; self.selection_current = False; self.measurement = None
        self.observation = None; self.receipt = None; self.error = None

    def fail(self, reason):
        if self.state != 'completed':
            self.error = str(reason); self.state = 'refused'

    def _fence(self, revision):
        if not self.in_tick():
            raise CaptureError('capture event requires population tick')
        if revision != self.policy_generation:
            raise CaptureError('capture source generation drift')

    def selecting(self, key, binding, revision):
        if key == self.identity and self.state not in {'completed', 'refused'}:
            self._fence(revision)
            self.selection_current = False

    def selected(self, key, binding, selection, revision):
        if key != self.identity or self.state in {'completed', 'refused'}:
            return
        self._fence(revision)
        expected = (self.source, self.slot['source_entry'], self.slot['native_reward']['kind'],
                    self.slot['native_reward']['index'], self.slot['item_entry'])
        actual = tuple(selection.get(f) for f in ('source_id', 'quest_id', 'kind', 'index', 'item'))
        if tuple(binding) != self.binding or actual != expected or selection.get('phase') != self.context['phase']:
            raise CaptureError('capture selected source/identity/slot/phase drift')
        if self.selection is not None and selection != self.selection:
            raise CaptureError('capture selected policy generation drift')
        import copy
        self.selection = copy.deepcopy(selection)
        self.selection_current = True

    def accepted(self, key, binding, quest, revision):
        if key != self.identity or self.state in {'completed', 'refused'}:
            return
        self._fence(revision)
        if tuple(binding) != self.binding or quest != self.slot['source_entry'] or self.selection is None or not self.selection_current:
            raise CaptureError('capture accepted goal lacks exact validated selection')
        if self.state == 'armed':
            self.state = 'pending'

    def tick(self, revision):
        self._fence(revision)
        if self.state != 'pending':
            return
        observation = capture_current_observations(self.population, self.context, self.identity,
            self.slot['source_entry'], self.snapshot, self.revision)
        actor = observation['data']['actor']['gear']
        if (actor['account_id'], actor['character_guid']) != self.binding:
            raise CaptureError('capture admitted actor durable identity drift')
        measurement = QuestAcquisitionMeasurement(self.population, self.context, self.identity,
            self.slot['key'], self.snapshot, self.revision, self.clock)
        self.observation = observation; self.measurement = measurement; self.state = 'observing'

    def rewarded(self, key, binding, quest, revision):
        if key != self.identity or self.state in {'completed', 'refused'}:
            return
        self._fence(revision)
        if self.state != 'observing' or tuple(binding) != self.binding or quest != self.slot['source_entry']:
            raise CaptureError('capture rewarded lifecycle identity/goal drift or interval not started')
        self.receipt = self.measurement.finish()
        self.state = 'completed'

    def lost(self, key, quest, revision):
        if key == self.identity and self.state not in {'completed', 'refused'}:
            self._fence(revision)
            raise CaptureError('capture goal lost/suspended/replaced: ' + str(quest))

    def drain(self, output):
        """OFF TICK: custody original value copies and diagnostics atomically; no invented readiness."""
        if self.in_tick():
            raise CaptureError('capture drain must be off tick')
        document = dict(schema='core1-finite-acquisition-lifecycle-v1', state=self.state,
            identity_key=self.identity, binding=list(self.binding), snapshot_id=self.snapshot,
            source_revision=self.revision, native_slot=self.slot, selection=self.selection,
            observation=self.observation, measurement=self.receipt, error=self.error,
            ready=False, missing=['native terminal reward transaction binding exact item/choice',
                'per-source accepted S01 acquisition policy authority', 'native travel/risk/contention witnesses'],
            scope='correlated admitted lifecycle only; not reward causality, earned/equip or complete semantics')
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        result = dict(state=self.state, ready=False, receipts={})
        with tempfile.TemporaryDirectory(prefix='.acquisition-drain-', dir=output.parent) as temporary:
            staged = Path(temporary) / 'receipts'; staged.mkdir()
            for name, value in (('lifecycle', document), ('observation', self.observation), ('measurement', self.receipt)):
                if value is None:
                    continue
                data = canonical(value) + b'\n'
                atomic_file(staged / (name + '.json'), data)
                result['receipts'][name] = dict(path=str((output / (name + '.json')).absolute()),
                    bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), generation=self.snapshot)
            # Same Linux no-replace publication contract as the accepted offline assembler.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(staged), -100, os.fsencode(output), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(output))
        return result



def normalize_quest_measurement(receipt, policy_descriptor, policy_root):
    """OFF TICK: authenticate original accepted policy and normalize a completed raw observation window."""
    import copy
    shape(receipt, {'snapshot_id', 'kind', 'source_revision', 'data'}, 'completed measurement receipt shape')
    if receipt['kind'] != 'measurement' or policy_descriptor.get('generation') != receipt['snapshot_id']:
        raise CaptureError('measurement accepted policy descriptor generation')
    data = receipt['data']
    if not isinstance(data, dict) or type(data.get('started_monotonic_ns')) is not int or type(data.get('ended_monotonic_ns')) is not int or not 0 <= data['started_monotonic_ns'] < data['ended_monotonic_ns'] <= 18446744073709551615 or type(data.get('observed_duration_ns')) is not int or data['observed_duration_ns'] != data['ended_monotonic_ns'] - data['started_monotonic_ns']:
        raise CaptureError('completed measurement time/overflow consistency')
    key = data['native_slot']['key']
    if data.get('slots') != {key: {}}:
        raise CaptureError('measurement must be unnormalized original slot interval')
    raw = read_descriptor(policy_descriptor, Path(policy_root))
    policy = decode(raw)
    if not isinstance(policy, dict) or policy.get('snapshot_id') != receipt['snapshot_id'] or policy.get('source_revision') != receipt['source_revision'] or policy.get('kind') != 'accepted_policy' or policy.get('data', {}).get('measurement_policy') != dict(metric='observed_quest_acquisition_window', field='attempt', rounding='ceil_seconds'):
        raise CaptureError('measurement accepted normalization policy provenance')
    seconds = (data['observed_duration_ns'] + 999999999) // 1000000000
    _uint(seconds)
    result = copy.deepcopy(receipt)
    result['data']['slots'][key]['attempt'] = seconds
    result['data']['normalization_policy'] = dict(descriptor=copy.deepcopy(policy_descriptor), original=policy)
    result['data']['missing'].remove('attempt normalization policy')
    return result



class NativeJournal:
    """Lossless native value revisions. Unchanged polls retain nothing; vectors store changed values once."""
    def __init__(self):
        self.entries=[]; self.header={}; self.order=[]; self.records={}

    def append(self,snapshot):
        import copy
        if not isinstance(snapshot,dict) or not isinstance(snapshot.get('attempts'),list):
            raise CaptureError('native journal snapshot shape')
        header={key:value for key,value in snapshot.items() if key!='attempts'}
        ids=[record['record_id'] for record in snapshot['attempts']]
        if len(ids)!=len(set(ids)):raise CaptureError('native journal duplicate record ID')
        entry=dict(schema='core1-native-journal-revision-v1',header_set={key:copy.deepcopy(value) for key,value in header.items() if key not in self.header or self.header[key]!=value},header_unset=[key for key in self.header if key not in header],records=[])
        if ids!=self.order:
            entry['record_order']=dict(append=ids[len(self.order):]) if ids[:len(self.order)]==self.order else dict(replace=ids)
        violation=None
        for record in snapshot['attempts']:
            identity=record['record_id'];old=self.records.get(identity,{})
            if old==record:continue
            patch=dict(record_id=identity,set={},unset=[key for key in old if key not in record],vectors={})
            for field,value in record.items():
                if field in old and old[field]==value:continue
                if isinstance(value,list):
                    before=old.get(field,[]);before=before if isinstance(before,list) else []
                    patch['vectors'][field]=dict(length=len(value),values=[[index,copy.deepcopy(item)] for index,item in enumerate(value) if index>=len(before) or before[index]!=item])
                else:patch['set'][field]=copy.deepcopy(value)
            entry['records'].append(patch)
            if old.get('terminal') is True and {k:v for k,v in old.items() if k!='current_context_matches'}!={k:v for k,v in record.items() if k!='current_context_matches'}:
                violation='native terminal record mutated'
            if 'events' in old and record.get('events',[])[:len(old['events'])]!=old['events']:
                violation='native event history changed/truncated'
            if 'reward_receipts' in old and record.get('reward_receipts',[])[:len(old['reward_receipts'])]!=old['reward_receipts']:
                violation='native reward receipt history changed/truncated'
            self.records[identity]=copy.deepcopy(record)
        changed=bool(entry['header_set'] or entry['header_unset'] or entry['records'] or 'record_order' in entry)
        if changed:
            self.entries.append(entry);self.header=copy.deepcopy(header);self.order=list(ids)
        # Preserve the offending original revision for custody, then refuse to treat it as valid.
        if violation:raise CaptureError(violation)
        return changed

    def latest(self):
        return dict(self.header,attempts=[self.records[identity] for identity in self.order])


def native_observations(data):
    """OFF-TICK iterator expanding either original v1 polls or compact v2 revisions, one at a time."""
    import copy
    schema=data.get('schema')
    if schema=='core1-original-native-quest-observations-v1':
        journal=NativeJournal()
        for snapshot in data['observations']:
            journal.append(snapshot)
            yield snapshot
        return
    if schema!='core1-original-native-quest-observations-v2':raise CaptureError('native original journal schema')
    header={};records={};order=[]
    for entry in data['observations']:
        shape(entry,{'schema','header_set','header_unset','records'} | ({'record_order'} if 'record_order' in entry else set()),'native journal revision fields')
        if entry['schema']!='core1-native-journal-revision-v1':raise CaptureError('native journal revision schema')
        if set(entry['header_set']) & set(entry['header_unset']):raise CaptureError('native journal conflicting header patch')
        header.update(copy.deepcopy(entry['header_set']))
        for field in entry['header_unset']:
            if field not in header:raise CaptureError('native journal absent header removal')
            del header[field]
        if 'record_order' in entry:
            change=entry['record_order']
            if set(change)=={'append'}:order+=change['append']
            elif set(change)=={'replace'}:order=list(change['replace'])
            else:raise CaptureError('native journal record order')
        for patch in entry['records']:
            shape(patch,{'record_id','set','unset','vectors'},'native journal record patch')
            if set(patch['set']) & (set(patch['unset']) | set(patch['vectors'])) or set(patch['unset']) & set(patch['vectors']):raise CaptureError('native journal conflicting record patch')
            identity=patch['record_id'];old=copy.deepcopy(records.get(identity,{}));record=records.setdefault(identity,{})
            record.update(copy.deepcopy(patch['set']))
            for field in patch['unset']:
                if field not in record:raise CaptureError('native journal absent record removal')
                del record[field]
            for field,vector in patch['vectors'].items():
                shape(vector,{'length','values'},'native journal vector patch')
                length=vector['length']
                if type(length) is not int or length<0:raise CaptureError('native journal vector length')
                previous=record.get(field,[]);old_length=len(previous) if isinstance(previous,list) else 0;value=previous[:length] if isinstance(previous,list) else []
                value.extend([None]*(length-len(value)));seen=set()
                for index,item in vector['values']:
                    if type(index) is not int or not 0<=index<length or index in seen:raise CaptureError('native journal vector index')
                    seen.add(index);value[index]=copy.deepcopy(item)
                if any(index>=old_length and index not in seen for index in range(length)):raise CaptureError('native journal missing appended value')
                record[field]=value
            if record.get('record_id')!=identity:raise CaptureError('native journal record identity patch')
            if old.get('terminal') is True and {k:v for k,v in old.items() if k!='current_context_matches'}!={k:v for k,v in record.items() if k!='current_context_matches'}:raise CaptureError('native terminal record mutated')
            if 'events' in old and record.get('events',[])[:len(old['events'])]!=old['events']:raise CaptureError('native event history changed/truncated')
            if 'reward_receipts' in old and record.get('reward_receipts',[])[:len(old['reward_receipts'])]!=old['reward_receipts']:raise CaptureError('native reward receipt history changed/truncated')
        if len(order)!=len(set(order)) or any(identity not in records for identity in order):raise CaptureError('native journal record census/order')
        yield dict(copy.deepcopy(header),attempts=[copy.deepcopy(records[identity]) for identity in order])


def atomic_file(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hard-link publication is atomic and refuses even a concurrently created destination.
    with tempfile.TemporaryDirectory(prefix='.native-capture-', dir=path.parent) as temporary:
        staged = Path(temporary) / 'capture'
        with staged.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.link(staged, path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)


def capture_population(population, output):
    """Call only from an existing authenticated fenced world-tick callback after deployment.

    This writes on the callback host; it is never called by the offline CLI or this task.
    SOAP printing is not a complete transport. Provider() enforces the native thread fence.
    """
    value = population.strategic_read_acquisition_catalog()
    catalogue(value)
    atomic_file(output, canonical(value) + b'\n')
    return dict(bytes=Path(output).stat().st_size,
                sha256=hashlib.sha256(Path(output).read_bytes()).hexdigest(),
                item_count=value['item_count'], quest_count=value['quest_count'],
                reward_count=value['reward_count'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--catalog'); mode.add_argument('--soap-url')
    p.add_argument('--sha256'); p.add_argument('--credentials')
    p.add_argument('--output', required=True); a = p.parse_args()
    try:
        if a.soap_url:
            if not a.credentials:
                raise CaptureError('SOAP credentials file required; never put secrets in command arguments')
            import importlib.util
            transport = DONOR.parents[4] / 'integration/cpi-351-api-activation-20261012/soap_transport.py'
            spec = importlib.util.spec_from_file_location('capture_soap_transport', transport)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            credentials = module.read_credentials(a.credentials)
            result = soap_capture(lambda command: module.soap_command(command, credentials, url=a.soap_url), a.output)
            sys.stdout.buffer.write(canonical(result) + b'\n')
            return 0
        if not a.sha256:
            raise CaptureError('externally pinned catalogue SHA256 required')
        source = Path(a.catalog).absolute()
        raw = read_descriptor(dict(path=str(source), bytes=source.stat().st_size, sha256=a.sha256), source.parent)
        atomic_file(a.output, canonical(inspect(decode(raw))) + b'\n')
        return 0
    except Exception as error:
        sys.stderr.write('REFUSED: ' + str(error) + '\n'); return 2



class NativeCaptureFrames:
    """One immutable public snapshot; standalone class deployable through existing python exec."""
    def __init__(self, population):
        import json
        import hashlib
        import secrets
        value = population.strategic_read_acquisition_catalog()
        if value.get('valid') is not True or value.get('reason') != '':
            raise ValueError('native catalogue observation refused')
        self.data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.token = secrets.token_hex(16)
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.generation = value['observation_generation']
        self.counts = {k: value[k] for k in ('item_count', 'quest_count', 'reward_count')}

    @classmethod
    def start(cls, namespace, population):
        if '_cpi225_native_capture' in namespace:
            raise ValueError('previous capture retained; resume or explicitly release it')
        value = cls(population)
        namespace['_cpi225_native_capture'] = value
        value.emit(value.header())

    @staticmethod
    def emit(value):
        import json
        text = json.dumps(value, separators=(',', ':'), ensure_ascii=True)
        # CommandScript truncates each line at 250 and then limits output to ten lines.
        print('\n'.join(text[i:i+240] for i in range(0, len(text), 240)))

    def header(self):
        return dict(protocol='cpi225-native-fragments-v1', token=self.token, total=len(self.data),
            sha256=self.sha256, generation=self.generation, counts=self.counts)

    def frame(self, token, offset, size):
        import base64
        import hashlib
        if token != self.token:
            raise ValueError('stale native capture token')
        if type(offset) is not int or type(size) is not int or not 0 <= offset < len(self.data) or size < 1:
            raise ValueError('invalid native capture cursor/size')
        data = self.data[offset:offset+size]
        return dict(**self.header(), offset=offset, next=offset+len(data), terminal=offset+len(data)==len(self.data),
            payload=base64.b64encode(data).decode('ascii'), chunk_sha256=hashlib.sha256(data).hexdigest())


class TruncatedFrame(CaptureError):
    pass


def parse_transport(response):
    """Decode actual CommandScript Output: lines; never use SOAP truncation as EOF."""
    if not isinstance(response, str):
        raise CaptureError('native transport response type')
    lines = response.splitlines()
    if any('lines truncated)' in line for line in lines):
        raise TruncatedFrame('SOAP native fragment truncated')
    if not lines or lines[0] != 'Output:':
        raise CaptureError('native transport command did not return Output')
    text = ''.join(lines[1:])
    try:
        value = decode(text.encode('utf-8'))
    except CaptureError as error:
        raise TruncatedFrame('native fragment JSON incomplete/corrupt') from error
    if not isinstance(value, dict):
        raise CaptureError('native transport envelope')
    return value


def assemble(header, fetch, initial_size=4096):
    """Fetch byte ranges with same-offset shrinking on truncation, without a total cap."""
    import base64
    fields = {'protocol', 'token', 'total', 'sha256', 'generation', 'counts'}
    shape(header, fields, 'native header fields')
    if header['protocol'] != 'cpi225-native-fragments-v1' or not isinstance(header['token'], str) or len(header['token']) != 32:
        raise CaptureError('native fragment protocol/token')
    if type(header['total']) is not int or header['total'] < 1 or type(header['generation']) is not int or header['generation'] < 1:
        raise CaptureError('native fragment header size/generation')
    shape(header['counts'], {'item_count', 'quest_count', 'reward_count'}, 'native header counts')
    _digest(header['sha256'])
    if any(type(n) is not int or n < 0 for n in header['counts'].values()):
        raise CaptureError('native header count values')
    if type(initial_size) is not int or initial_size < 1:
        raise CaptureError('native fragment requested size')
    chunks = []; offset = 0; size = initial_size
    while offset < header['total']:
        try:
            frame = fetch(header['token'], offset, size)
        except TruncatedFrame:
            if size == 1:
                raise CaptureError('transport cannot fit one-byte native frame')
            size = max(1, size // 2)
            continue
        shape(frame, fields | {'offset', 'next', 'terminal', 'payload', 'chunk_sha256'}, 'native frame fields')
        if any(canonical(frame[k]) != canonical(header[k]) for k in fields):
            raise CaptureError('mixed/stale native fragment identity')
        if type(frame['offset']) is not int or frame['offset'] != offset or type(frame['next']) is not int or not offset < frame['next'] <= min(offset+size, header['total']):
            raise CaptureError('native fragment cursor')
        if type(frame['terminal']) is not bool or frame['terminal'] != (frame['next'] == header['total']):
            raise CaptureError('native fragment terminal marker')
        try:
            data = base64.b64decode(frame['payload'], validate=True)
        except (ValueError, TypeError) as error:
            raise CaptureError('native fragment payload encoding') from error
        if len(data) != frame['next']-offset or hashlib.sha256(data).hexdigest() != frame['chunk_sha256']:
            raise CaptureError('native fragment byte custody')
        chunks.append(data); offset = frame['next']
    raw = b''.join(chunks)
    if len(raw) != header['total'] or hashlib.sha256(raw).hexdigest() != header['sha256']:
        raise CaptureError('native complete snapshot digest')
    value = decode(raw); catalogue(value)
    if any(value[k] != header['counts'].get(k) for k in ('item_count', 'quest_count', 'reward_count')) or value['observation_generation'] != header['generation']:
        raise CaptureError('native complete snapshot counts/generation')
    return raw


def soap_capture(command, output):
    """Run only after Root's explicit native deployment/capture grant; command is authenticated SOAP."""
    import base64
    import inspect
    if os.path.lexists(output):
        raise CaptureError('output must not exist')
    source = inspect.getsource(NativeCaptureFrames)
    encoded = base64.b64encode(source.encode()).decode('ascii')
    # The only native call is snapshot initialization. Subsequent frames read immutable Python bytes.
    begin = "exec(__import__('base64').b64decode(" + repr(encoded) + "));NativeCaptureFrames.start(globals(),__import__('azerothcore'))"
    def invoke(code):
        result = command('python exec ' + code)
        if result.get('ok') is not True:
            raise CaptureError('authenticated SOAP capture command refused')
        return parse_transport(result['response'])
    header = invoke(begin)
    def fetch(token, offset, size):
        return invoke("_cpi225_native_capture.emit(_cpi225_native_capture.frame(" + repr(token) + ',' + str(offset) + ',' + str(size) + "))")
    raw = assemble(header, fetch)
    # Existing snapshots stay intact if local validation/output publication fails; caller can resume with token.
    atomic_file(output, raw)
    return dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), header=header,
        retained_remote='_cpi225_native_capture immutable public bytes; delete only this variable after acceptance')


if __name__ == '__main__':
    raise SystemExit(main())
