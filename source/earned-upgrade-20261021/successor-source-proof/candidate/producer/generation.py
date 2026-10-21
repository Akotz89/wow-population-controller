#!/usr/bin/env python3
"""Sealed raw/native acquisition facts -> existing canonical documents, never Root acceptance."""
import argparse
import copy
import importlib.util
import sys
import tempfile
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from ingest_capture import CaptureError, _uint
from effective_data import canonical, measurement, load_inputs, validate_documents, FAMILIES

REPO = Path(__file__).resolve().parent
SCHEMAS = REPO / 'schemas'
_spec = importlib.util.spec_from_file_location('content_compiler', REPO / 'compile_content_graph.py')
compiler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compiler)

def digest(value):
    return measurement(value)['sha256']


def build(m, tables, policy, native):
    b = m['binding']; generation = digest(b)
    native_slots = {slot['key']: slot for slot in native['slot_custody']['slots']}
    availability = dict(minimum_frontier=1, maximum_frontier=80, phase_mask=1, spawn_mask=1, event_ids=[], pool_ids=[], condition_id=None)
    nodes = {k: {} for k in compiler.NODE_KINDS}
    conditions = {}; edges = []; projection = defaultdict(list); incompatibilities = []; unresolved = list(native['unresolved'])
    def node(kind, identity, attrs=None):
        _uint(identity)
        if identity == 0:
            raise CaptureError('zero native node identity')
        if identity not in nodes[kind]:
            nodes[kind][identity] = dict(id=identity, availability=copy.deepcopy(availability), attributes=attrs or {})
    current_1_19 = policy['stages'][0]['name'] == '1_19' and [s['id'] for s in policy['stages'] if s['loaded']] == ['S01']
    item_ids = set()
    for item in native['items']:
        if not isinstance(item, dict) or set(item) != {'entry', 'attributes', 'no_source_reason'} or item['entry'] in item_ids or not isinstance(item['attributes'], dict) or not isinstance(item['no_source_reason'], str):
            raise CaptureError('effective item identity/shape')
        item_ids.add(item['entry']); node('item', item['entry'], item['attributes'])
    # A05 source-rule catalogues require exact ItemTemplate leaves at item_node locus.
    # Source-edge leaves cannot stand in for an item-owner restriction in the native query contract.
    predicate_items={slot['item_entry'] for slot in native_slots.values() if slot['native_quest'].get('source_rule_version')=='core1-ordinary-quest-source-rules-v1'}
    for identity in predicate_items:
        attrs=nodes['item'][identity]['attributes'];leaves=[]
        for field,kind in (('allowable_class',15),('allowable_race',16)):
            leaves.append(dict(op='leaf',condition_type=kind,values=[attrs[field],0,0]))
        if attrs['required_skill']:leaves.append(dict(op='indeterminate'))  # Native item effective skill includes bonuses; ConditionMgr7 is base+HasSkill.
        if attrs['required_level']:leaves.append(dict(op='leaf',condition_type=27,values=[attrs['required_level'],3,0]))
        expression=leaves[0] if len(leaves)==1 else dict(op='all',arguments=sorted(leaves,key=canonical))
        identity_condition='condition:'+digest(expression)
        conditions[identity_condition]=dict(condition_id=identity_condition,expression=expression)
        nodes['item'][identity]['availability']['condition_id']=identity_condition
    reverse = Counter(); provenance = Counter(); seen = set(); craft_graph = defaultdict(set)
    for f in sorted(native['acquisitions'], key=lambda f: f['key']):
        key = f['key']
        if not isinstance(key, str) or not key or key in seen:
            raise CaptureError('duplicate/invalid native source key')
        seen.add(key)
        if f['family'] not in FAMILIES:
            unresolved.append(dict(source=key, reason='unsupported_family', native=f)); continue
        if f['item_entry'] not in item_ids:
            raise CaptureError('source item missing effective export')
        kind, edge_kind = FAMILIES[f['family']]
        if kind == 'item' and f['source_entry'] not in item_ids:
            raise CaptureError('native source item missing effective export')
        node(kind, f['source_entry'])
        if not isinstance(f['frontier'], list) or len(f['frontier']) != 2 or any(type(x) is not int for x in f['frontier']) or not 1 <= f['frontier'][0] <= f['frontier'][1] <= 80:
            raise CaptureError('native frontier interval')
        if not isinstance(f['stages'], list) or not f['stages'] or f['stages'] != sorted(f['stages']) or len(f['stages']) != len(set(f['stages'])) or any(s not in {s['id'] for s in policy['stages']} for s in f['stages']):
            raise CaptureError('source stage identities')
        if any(not policy['stages'][int(s[1:])-1]['input_complete'] for s in f['stages']):
            raise CaptureError('source claims missing stage inputs')
        if not isinstance(f['probability'], list) or len(f['probability']) != 2:
            raise CaptureError('native probability ratio')
        n, d = f['probability']
        if type(n) is not int or type(d) is not int or not 0 < n <= d:
            raise CaptureError('native probability range')
        p = Fraction(n, d)
        if p.numerator > 1000000 or p.denominator > 1000000:
            raise CaptureError(f"selected schema probability incompatibility source={key} chance_numerator={p.numerator} chance_denominator={p.denominator}; exact values retained in sealed native input, no rounding permitted")
        if not 0 < _uint(f['minimum_count']) <= _uint(f['maximum_count']):
            raise CaptureError('native yield range')
        current_stage = current_1_19 and f['stages'] == ['S01']
        expression = copy.deepcopy(f['condition'])
        if f['unresolved'] or f['variant'] or not current_stage:
            unknown = dict(op='indeterminate')
            expression = unknown if expression is None else dict(op='all', arguments=[expression, unknown])
        condition = None if expression is None else 'condition:' + digest(expression)
        if condition:
            conditions[condition] = dict(condition_id=condition, expression=expression)
        av = dict(minimum_frontier=f['frontier'][0], maximum_frontier=f['frontier'][1], phase_mask=f['phase_mask'], spawn_mask=f['spawn_mask'], event_ids=sorted(f['event_ids']), pool_ids=sorted(f['pool_ids']), condition_id=condition)
        src = dict(kind=kind, id=f['source_entry']); target = dict(kind='item', id=f['item_entry'])
        edge_id = 'source:' + digest(key)
        deps = Counter(); eligibility = {}
        for reagent in f['reagents']:
            if not isinstance(reagent, list) or len(reagent) != 4 or reagent[0] not in item_ids or not _uint(reagent[1]) or any(type(v) is not bool for v in reagent[2:]):
                raise CaptureError('native reagent identity/count')
            deps[reagent[0]] += reagent[1]
            old = eligibility.get(reagent[0], (True, True))
            eligibility[reagent[0]] = (old[0] and reagent[2], old[1] and reagent[3])
            _uint(deps[reagent[0]])
        if deps and f['family'] != 'craft':
            raise CaptureError('noncraft native reagent relation')
        for identity, count in sorted(deps.items()):
            craft_graph[f['item_entry']].add(identity)
            reverse[('recipe_reagent', kind, f['source_entry'], identity, 'recipe_reagent')] += 1
            provenance[('normalized.recipe_reagent', f['source_entry'], identity)] += 1
            edges.append(dict(edge_id=edge_id+'.reagent.'+str(identity), kind='recipe_reagent', **{'from':src,'to':dict(kind='item', id=identity)}, semantics='recipe_reagent', availability=copy.deepcopy(av), attributes=dict(count=count, native_source_key=key), provenance=dict(source='normalized.recipe_reagent', natural_key=[f['source_entry'], identity])))
        dependencies = sorted([dict(item_id=i, count=c, available=eligibility[i][0], phase_authorized=eligibility[i][1], earned=True) for i,c in deps.items()], key=canonical)
        # These are captured catalogue predicates, never inventory ownership or reward grants.
        s = dict(source_id=edge_id, source_kind=f['family'], source_ref=src, minimum_count=f['minimum_count'], maximum_count=f['maximum_count'], chance_numerator=p.numerator, chance_denominator=p.denominator, minimum_phase=f['frontier'][0], maximum_phase=f['frontier'][1], required_group_size=f['group_size'], money_cost_copper=f['copper'], travel_seconds=f['travel'], seconds_per_attempt=f['attempt'], risk_milli=f['risk'], contention_milli=f['contention'], required_capabilities=sorted(f['capabilities']), required_profession_skill_id=f['profession'][0], required_profession_skill_value=f['profession'][1], quest_alternative_group=f['alternative'], craft_dependencies=dependencies, condition_root=condition, earned=True)
        projection[f['item_entry']].append(s)
        attributes = dict(native_acquisition=copy.deepcopy(f), generation=generation)
        natural_key = [f['source_entry'], f['item_entry']]
        if f['family'] in {'quest_reward', 'dungeon_quest'}:
            slot = native_slots[key]; reward = slot['native_reward']
            reward_kind = 'guaranteed' if reward['kind'] == 'fixed' else 'choice'
            natural_key += [reward_kind, reward['index'] + 1]
            attributes.update(native_custody=copy.deepcopy(slot), native_table='quest_template',quest_id=f['source_entry'],
                reward_kind=reward_kind,native_reward_index=reward['index'],sql_reward_column_suffix=reward['index']+1,
                native_item_column=('RewardItem' if reward_kind == 'guaranteed' else 'RewardChoiceItemID') + str(reward['index']+1),
                minimum_count=f['minimum_count'],maximum_count=f['maximum_count'])
        edges.append(dict(edge_id=edge_id, kind=edge_kind, **{'from':src,'to':target}, semantics=f['family'], availability=av, attributes=attributes, provenance=dict(source='normalized.'+f['family'], natural_key=natural_key)))
        reverse[(edge_kind, kind, *natural_key, f['family'])] += 1
        provenance[('normalized.'+f['family'], *natural_key)] += 1
        if f['minimum_count'] != 1 or f['maximum_count'] != 1:
            incompatibilities.append(dict(source=key, field='minimum_count/maximum_count', actual=[f['minimum_count'],f['maximum_count']], consumer_error='unsupported_nonunit_yield'))
        if not current_stage:
            incompatibilities.append(dict(source=key, field='stages', actual=f['stages'], consumer_error='no_native_stage_identity_field'))
        for field, value in sorted(f['variant'].items()):
            incompatibilities.append(dict(source=key, field='variant.'+field, actual=value, consumer_error='no_native_variant_predicate_field'))
        stochastic = {'creature_loot','gameobject_loot','dungeon_drop','disenchant','prospecting','milling','fishing','skinning'}
        if p < 1 and (f['family'] not in stochastic or f['attempt'] <= 0):
            incompatibilities.append(dict(source=key, field='probability/source_kind/seconds_per_attempt', actual=[p.numerator,p.denominator,f['family'],f['attempt']], consumer_error='invalid_source_data'))
        if f['family'] == 'craft' and (not deps or f['profession'][0] == 0):
            incompatibilities.append(dict(source=key, field='craft_dependencies/required_profession_skill_id', actual=[len(deps),f['profession'][0]], consumer_error='invalid_source_data'))
        if f['unresolved']:
            incompatibilities.append(dict(source=key, field='unresolved', actual=f['unresolved'], consumer_error='condition_unknown'))
            unresolved.append(dict(source=key, reason='native_unresolved', details=f['unresolved']))
    for identity, count in reverse.items():
        if count > 1:
            incompatibilities.append(dict(field='reverse_identity', identity=list(identity), actual=count, consumer_error='ambiguous_reverse_identity'))
    for identity, count in provenance.items():
        if count > 1:
            incompatibilities.append(dict(field='provenance', identity=list(identity), actual=count, consumer_error='ambiguous_provenance'))
    # Iterative cycle walk preserves complete graphs even when the selected loader refuses them.
    state = {}; cycles = set()
    for root in craft_graph:
        stack = [(root, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                state[current] = 2; continue
            if state.get(current) == 1:
                cycles.add(current); continue
            if state.get(current) == 2:
                continue
            state[current] = 1; stack.append((current, True))
            stack.extend((x, False) for x in sorted(craft_graph.get(current, ()), reverse=True))
    if cycles:
        incompatibilities.append(dict(field='craft_dependencies', actual=sorted(cycles), consumer_error='recipe_dependency_cycle'))
    # Match visitReagent's call order and depth-before-memo check; graph size is unrestricted.
    native_state = {}; native_failure = None
    for item in sorted(projection):
        for option in sorted(projection[item], key=lambda s:s['source_id']):
            stack = [(item, 0, False)]
            while stack:
                current, depth, leaving = stack.pop()
                if leaving:
                    native_state[current] = 2; continue
                if depth > 128:
                    native_failure = ('depth', depth); break
                if native_state.get(current) == 1:
                    native_failure = ('cycle', current); break
                if native_state.get(current) == 2:
                    continue
                native_state[current] = 1; stack.append((current, depth, True))
                stack.extend((child, depth+1, False) for child in sorted(craft_graph.get(current, ()), reverse=True))
            if native_failure:
                break
        if native_failure:
            break
    if native_failure and native_failure[0] == 'depth':
        incompatibilities.append(dict(field='craft_dependencies', actual=native_failure[1], consumer_error='recipe_dependency_depth'))
    item_dispositions = []
    for item in native['items']:
        count = len(projection[item['entry']])
        if not count and not item['no_source_reason']:
            raise CaptureError('effective item without source/disposition: '+str(item['entry']))
        if count and item['no_source_reason']:
            raise CaptureError('contradictory item no-source reason')
        item_dispositions.append(dict(item_id=item['entry'], sources=count, reason=item['no_source_reason']))
    graph = dict(schema_version='cpi-207-content-graph-v1', **{k:b[k] for k in ('core_revision','playerbots_revision','progression_revision','content_revision','world_dump_digest','dbc_set_digest')}, availability_model='realm_frontier+phase_mask+spawn_mask+event_pool+condition_ast', nodes={compiler.NODE_KINDS[k]:list(v.values()) for k,v in nodes.items()}, condition_roots=list(conditions.values()), edges=edges)
    graph, gd = compiler.compile_graph(graph)
    proj = [dict(schema_version='cpi-207-item-source-v1', content_revision=b['content_revision'], graph_digest=gd, item_id=i, sources=sorted(projection[i],key=lambda s:s['source_id'])) for i in sorted(item_ids)]
    source = {k:b[k] for k in ('candidate_id','core_revision','playerbots_revision','progression_revision','world_dump_digest','config_digest','dbc_set_digest')}
    manifest = dict(schema_version='cpi-207-content-manifest-v1', compiler_revision=b['compiler_revision'], started_at=b['captured_at'], completed_at=b['captured_at'], source=source, migrations=[dict(ordinal=d['ordinal'],name=d['name'],digest=d['sha256']) for d in m['migrations']], dbc_files=sorted([dict(name=d['name'],digest=d['sha256']) for d in m['dbc_files']],key=lambda d:d['name']), row_counts={k:len(v) for k,v in tables.items()}, warning_counts=dict(unresolved=len(unresolved), consumer_incompatibilities=len(incompatibilities), missing_stages=sum(not s['input_complete'] for s in policy['stages'])), graph_digest=gd, reproducible_peer_digest=gd)
    documents = dict(graph=graph, manifest=manifest, projection=proj)
    validate_documents(documents)
    stage_counts = {stage['id']:({family:sum(f['family']==family and stage['id'] in f['stages'] for f in native['acquisitions']) for family in sorted(native['family_dispositions'])} if stage['input_complete'] else None) for stage in policy['stages']}
    report = dict(schema='core1-generation-evidence-v1', input_generation=generation, effective_dataset_accepted=False, loaded_generation_verified=native['loaded_generation_verified'], stages=policy['stages'], stage_source_counts=stage_counts, raw_row_dispositions=native['dispositions'], item_dispositions=sorted(item_dispositions,key=lambda x:x['item_id']), family_dispositions=native['family_dispositions'], unresolved=unresolved, consumer_incompatibilities=incompatibilities, measurements={k:measurement(v) for k,v in documents.items()}, native_input_digest=digest(native), consumer_generation=digest(dict(source=source,compiler_revision=manifest['compiler_revision'],migrations=manifest['migrations'],dbc_files=manifest['dbc_files'],content_revision=graph['content_revision'])), fullcurrent_complete=False, full14_complete=False, full37_complete=False, consumer_compatible=None, selected_consumer_checks_passed=not incompatibilities, consumer_validation='not_run')
    return documents, report


def produce(path, expected):
    return build(*load_inputs(path, expected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True); parser.add_argument('--sha256',required=True); parser.add_argument('--output',required=True)
    args = parser.parse_args()
    try:
        documents, report = produce(args.manifest,args.sha256)
        output = Path(args.output)
        if output.exists():
            raise CaptureError('output must not exist; no partial accepted overwrite')
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.generation-', dir=output.parent) as temporary:
            staged = Path(temporary) / 'documents'
            staged.mkdir()
            for role,value in {**documents,'evidence':report}.items():
                (staged/(role+'.json')).write_bytes(canonical(value)+b'\n')
            staged.rename(output)
        return 0
    except Exception as error:
        sys.stderr.write('REFUSED: '+str(error)+'\n'); return 2

if __name__ == '__main__':
    raise SystemExit(main())
