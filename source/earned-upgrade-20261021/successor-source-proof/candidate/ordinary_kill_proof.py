"""OFF TICK exact raw-to-loaded ordinary kill proof; never an effect authorization."""
import copy
import hashlib
import json
from pathlib import Path
from capture_native_acquisition import CaptureError, canonical, digest, read_descriptor, decode

TABLES = ('quest_template', 'quest_template_addon', 'creature_template', 'creature',
          'quest_poi', 'quest_poi_points', 'creature_template_addon', 'creature_addon',
          'smart_scripts', 'game_event_creature', 'pool_creature', 'spawn_group')
MISSING_BINDING = ('Native source query/action tokens do not bind authored addon flags, complete target '
                   'arrays, target template/spawn/POI, or the fifth kill-credit hook registry and reload '
                   'generation; this input establishes neither action-time revalidation nor authenticated enforced no-reload custody')


def require(value, reason):
    if not value:
        raise CaptureError(reason)


def integer(row, name):
    value = row[name]
    if isinstance(value, str) and value.isascii() and value.lstrip('-').isdecimal():
        value = int(value)
    require(type(value) is int, 'raw integer field: ' + name)
    return value


def census_pin():
    path = Path(__file__).with_name('ordinary-kill-census-pin.json')
    pin = decode(path.read_bytes())
    raw = read_descriptor(pin, path.parent)
    return pin, decode(raw)


def _classify(quest, selected):
    """Pure classifier over exact selected rows. Public derive supplies authenticated full censuses."""
    qid = quest['entry']
    def rows(table):
        return [record['row'] for record in selected[table]]
    def single(table):
        values = rows(table)
        require(len(values) == 1, 'exact one raw ' + table + ' row required')
        return values[0]
    raw, addon = single('quest_template'), single('quest_template_addon')
    require(integer(raw, 'ID') == integer(addon, 'ID') == qid, 'raw quest/addon identity')
    require(integer(raw, 'QuestType') == 2, 'ordinary raw QuestType method must be 2')
    require(integer(raw, 'QuestInfoID') == quest['type'] == 0, 'raw QuestInfoID/loaded type drift')
    flags, special = integer(raw, 'Flags'), integer(addon, 'SpecialFlags')
    require(flags == quest['flags'], 'raw main Flags/loaded flags drift')
    # Other flag combinations need their own source reading before joining this proved subset.
    require(0 <= flags and not flags & ~0x8, 'unexamined main quest flags outside none/sharable')
    require(special == 0, 'authored addon flags outside the proved clear CAST/exploration domain')
    require(quest['cast_objective'] is True and quest['exploration_or_event'] is False,
            'loaded KILL|CAST|SPEAKTO/exploration transformation drift')
    expected = [dict(index=i, entry=integer(raw, 'RequiredNpcOrGo'+str(i+1)),
                     count=integer(raw, 'RequiredNpcOrGoCount'+str(i+1))) for i in range(4)]
    require(canonical(quest['required_targets']) == canonical(expected), 'complete raw/loaded four-target array drift')
    positive = [r for r in expected if r['entry'] > 0 and 0 < r['count'] <= 65535]
    require(len(positive) == 1 and all(r in positive or r['entry'] == r['count'] == 0 for r in expected),
            'ordinary proof requires one positive creature objective and three zero/zero slots')
    target = positive[0]
    for field, count, first, second in [('required_items', 6, 'RequiredItemId', 'RequiredItemCount'),
                                       ('required_source_items', 4, 'ItemDrop', 'ItemDropQuantity')]:
        exact = [dict(index=i, item=integer(raw, first+str(i+1)), count=integer(raw, second+str(i+1))) for i in range(count)]
        require(canonical(quest[field]) == canonical(exact) and all(r['item'] == r['count'] == 0 for r in exact),
                'raw/loaded item/source-item objective excluded or drift: ' + field)
    joins = {'MinLevel':'minimum_level', 'QuestLevel':'native_quest_level', 'SuggestedGroupNum':'native_suggested_players',
             'AllowableRaces':'allowable_races', 'TimeAllowed':'time_allowed', 'StartItem':'source_item',
             'RequiredPlayerKills':'required_player_kills', 'RewardNextQuest':'next_quest_in_chain',
             'RequiredFactionId1':'rep_objective_faction', 'RequiredFactionValue1':'rep_objective_value',
             'RequiredFactionId2':'rep_objective_faction2', 'RequiredFactionValue2':'rep_objective_value2'}
    addon_joins = {'MaxLevel':'maximum_level', 'AllowableClasses':'required_classes', 'SourceSpellID':'source_spell',
                   'PrevQuestID':'previous_quest_id', 'NextQuestID':'next_quest_id', 'ExclusiveGroup':'exclusive_group',
                   'BreadcrumbForQuestId':'breadcrumb_quest', 'RewardMailTemplateID':'reward_mail_template',
                   'RequiredSkillID':'required_skill', 'RequiredSkillPoints':'required_skill_rank',
                   'RequiredMinRepFaction':'minimum_rep_faction', 'RequiredMinRepValue':'minimum_rep_value',
                   'RequiredMaxRepFaction':'maximum_rep_faction', 'RequiredMaxRepValue':'maximum_rep_value',
                   'ProvidedItemCount':'source_item_count'}
    for source, mapping in ((raw, joins), (addon, addon_joins)):
        require(all(integer(source, column) == quest[field] for column, field in mapping.items()), 'raw/loaded native prerequisite domain drift')
    require(integer(addon, 'RewardMailDelay') == 0, 'reward mail delay unsupported')
    excluded = ('previous_quest_id', 'next_quest_in_chain', 'exclusive_group', 'breadcrumb_quest',
                'minimum_rep_faction', 'maximum_rep_faction', 'source_spell', 'source_item', 'source_item_count',
                'time_allowed', 'reward_mail_template', 'required_skill', 'required_skill_rank',
                'rep_objective_faction', 'rep_objective_value', 'rep_objective_faction2', 'rep_objective_value2',
                'required_player_kills', 'timed', 'repeatable', 'daily', 'weekly', 'monthly', 'seasonal',
                'dungeon_finder', 'previous_quests', 'previous_chain_quests', 'exclusive_quests', 'breadcrumb_dependents')
    require(not any(quest[field] for field in excluded), 'existing unsupported completion/prerequisite semantics')
    require(not any(integer(row, 'entry') != target['entry'] and
                    any(integer(row, key) == target['entry'] for key in ('KillCredit1', 'KillCredit2'))
                    for row in rows('creature_template')), 'incoming target KillCredit alias semantics unresolved')
    creature = single('creature_template')
    require(integer(creature, 'entry') == target['entry'], 'target template identity')
    require(creature['AIName'] == creature['ScriptName'] == '' and
            not any(integer(creature, key) for key in ('KillCredit1', 'KillCredit2', 'difficulty_entry_1', 'difficulty_entry_2', 'difficulty_entry_3')),
            'target AI/script/alias/difficulty semantics unresolved')
    spawns = rows('creature')
    require(bool(spawns), 'missing target spawns')
    require(all(integer(s, 'id') == target['entry'] and not s['ScriptName'] and integer(s, 'phaseMask') > 0 and integer(s, 'spawnMask') > 0 for s in spawns),
            'target spawn/script/mask semantics unresolved')
    require(not any(rows(t) for t in ('smart_scripts', 'game_event_creature', 'pool_creature', 'spawn_group')),
            'target script/event/pool/spawn-group semantics unresolved')
    require(all(not r['auras'] and not any(integer(r, key) for key in ('mount', 'path_id', 'emote'))
                for table in ('creature_template_addon', 'creature_addon') for r in rows(table)), 'target addon aura/path/emote semantics unresolved')
    pois, points = rows('quest_poi'), rows('quest_poi_points')
    require(bool(quest['giver_spawns']), 'missing native giver map/phase/spawn closure')
    routes = []
    for poi in pois:
        objective = integer(poi, 'ObjectiveIndex')
        if objective not in (target['index'], -1):
            raise CaptureError('mixed/unsupported objective POI')
        attached = [p for p in points if integer(p, 'Idx1') == integer(poi, 'id')]
        require(bool(attached), 'missing objective/reward POI points')
        require(all(integer(p, 'QuestID') == qid for p in attached), 'POI point quest drift')
        for point in attached:
            integer(point, 'X'); integer(point, 'Y')
        if any(integer(poi, 'MapID') == g['map_id'] == integer(s, 'map') and
               g['phase_mask'] & integer(s, 'phaseMask') and g['spawn_mask'] & integer(s, 'spawnMask')
               for g in quest['giver_spawns'] for s in spawns):
            routes.append(objective)
    require(target['index'] in routes and -1 in routes, 'missing compatible target/reward POI/map/phase/spawn closure')
    require(len({(integer(r, 'QuestID'), integer(r, 'id')) for r in pois}) == len(pois) and
            len({(integer(r, 'QuestID'), integer(r, 'Idx1'), integer(r, 'Idx2')) for r in points}) == len(points) and
            all(any(integer(p, 'Idx1') == integer(r, 'id') for r in pois) for p in points), 'POI identity/point join drift')
    return dict(flag_domains=dict(raw_quest_method=integer(raw, 'QuestType'), raw_quest_type=integer(raw, 'QuestInfoID'),
                     raw_main_flags=flags, authored_addon_special_flags=special, loaded_cast_objective=quest['cast_objective'],
                     loaded_exploration_or_event=quest['exploration_or_event']),
                target_entry=target['entry'], target_index=target['index'], loaded_targets=copy.deepcopy(expected))


def _select(inputs, root, quest):
    """Verify complete tables once; retain only relevant exact rows and their original ordinals."""
    selected = {}; qid = quest['entry']; targets = {r['entry'] for r in quest['required_targets'] if r['entry'] > 0}
    guids = set()
    for table in TABLES:
        descriptor = inputs['tables'][table]
        require(descriptor['generation'] == inputs['binding']['snapshot_id'], 'raw table generation')
        raw = read_descriptor(descriptor, root); records = []; count = 0
        for ordinal, line in enumerate(line for line in raw.splitlines() if line):
            row = decode(line); count += 1
            require(set(row) == set(descriptor['columns']), 'raw table columns')
            if table in ('quest_template', 'quest_template_addon'): match = integer(row, 'ID') == qid
            elif table == 'creature_template': match = integer(row, 'entry') in targets or any(integer(row, key) in targets for key in ('KillCredit1', 'KillCredit2'))
            elif table == 'creature': match = integer(row, 'id') in targets
            elif table in ('quest_poi', 'quest_poi_points'): match = integer(row, 'QuestID') == qid
            elif table == 'creature_template_addon': match = integer(row, 'entry') in targets
            elif table in ('creature_addon', 'game_event_creature', 'pool_creature'): match = integer(row, 'guid') in guids
            elif table == 'spawn_group': match = integer(row, 'spawnType') == 0 and integer(row, 'spawnId') in guids
            else: match = integer(row, 'source_type') == 0 and (integer(row, 'entryorguid') in targets or -integer(row, 'entryorguid') in guids)
            if match:
                records.append(dict(raw_ref=[table, ordinal, digest(row)], row=row))
                if table == 'creature': guids.add(integer(row, 'guid'))
        require(count == descriptor['rows'], 'complete raw table census count')
        selected[table] = records
    return selected


def derive(quest, context, input_path, expected_input_sha):
    path = Path(input_path).absolute()
    descriptor = dict(path=str(path), bytes=path.stat().st_size, sha256=expected_input_sha)
    result = dict(schema='ordinary-positive-creature-target-proof-v1', input_manifest=descriptor,
                  quest_entry=quest['entry'], static_status='UNKNOWN', missing=[], raw_rows=[], table_descriptors={},
                  loaded_targets=copy.deepcopy(quest['required_targets']), effect_custody=dict(status='UNKNOWN', missing_native_binding=MISSING_BINDING))
    try:
        inputs = decode(read_descriptor(descriptor, path.parent)); b = inputs['binding']; snapshot = b['snapshot_id']
        from capture_manifest import compiler_identity
        require(b['compiler_revision'] == compiler_identity(), 'current caller/compiler identity drift')
        for source in inputs['source_files']:
            require(source['generation'] == snapshot, 'source file generation drift')
            read_descriptor(source, path.parent)
        descriptor['generation'] = snapshot
        catalog = decode(read_descriptor(inputs['catalog'], path.parent))
        attestation = decode(read_descriptor(inputs['attestation'], path.parent)); revision = digest(inputs['source_files'])
        require(digest(inputs['tables']) == b['world_dump_digest'] and attestation['binding_sha256'] == digest(b), 'input/raw/binding custody drift')
        require(attestation['source_files_digest'] == attestation['exporter_revision'] == revision and
                attestation['catalog_sha256'] == inputs['catalog']['sha256'] and
                attestation['observation_generation'] == catalog['observation_generation'] == context['observation_generation'] and
                attestation['phase'] == catalog['phase'] == context['phase'], 'catalogue/source/phase generation drift')
        require([q for q in catalog['quests'] if q['entry'] == quest['entry']] == [quest], 'original loaded quest drift')
        result.update(original_catalogue_sha256=inputs['catalog']['sha256'], observation_generation=catalog['observation_generation'],
                      source_files_digest=revision, phase=copy.deepcopy(catalog['phase']))
        selected = _select(inputs, path.parent, quest)
        result.update(table_descriptors={t:copy.deepcopy(inputs['tables'][t]) for t in TABLES},
                      raw_rows=[r for table in TABLES for r in selected[table]])
        result.update(_classify(quest, selected))
        pin, census = census_pin(); result['census'] = dict(pin,generation=snapshot)
        result['effect_custody']['census_missing']=copy.deepcopy(census['runtime_binding']['missing'])
        # The census worker owns the independent selected source/inheritance proof.
        validate_census(census, inputs, path.parent)
        result['static_status'] = 'PROVEN'
    except (CaptureError, OSError, KeyError, TypeError, ValueError) as error:
        result['missing'].append(str(error))
    return result


def census_sources(census):
    """Exact files needed to retain the independently pinned census and semantic readings."""
    found = {}
    def walk(value):
        if isinstance(value, dict):
            if {'path', 'sha256', 'bytes'} <= set(value):
                found[value['path']] = {key:value[key] for key in ('path','sha256','bytes')}
            else:
                for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    for key in ('methods', 'classes', 'source_references', 'loader_sources'):
        walk(census[key])
    for key in ('composition','selected_source_inventory','selected_vfs','source_seal','candidate_manifest','engine_observation_capabilities'):
        walk(census['bindings'][key])
    return [found[name] for name in sorted(found)]


def validate_census(census, inputs, root):
    require(census['schema'] == 'core1-selected-player-script-five-hook-census-v1' and
            census['source_only'] is True and not census['unresolved'], 'selected five-hook source census incomplete')
    binding = census['bindings']; complete = census['completeness']
    require(binding['complete_source_identity'] == inputs['binding']['core_revision'] and
            inputs['binding']['candidate_id'] in {binding['complete_source_identity'], 'candidate:'+binding['complete_source_identity']},
            'selected native source identity differs from original input')
    require(complete['status'] == 'COMPLETE_SELECTED_SOURCE_CENSUS', 'selected source census not complete')
    composition = decode(read_descriptor(binding['composition'], root))
    inventory = decode(read_descriptor(binding['selected_source_inventory'], root))
    require(digest(composition['source']) == composition['complete_source_identity'] == binding['complete_source_identity'] == inventory['complete_source_identity'],
            'selected complete source identity derivation')
    indexed = {row['logical_path']:{key:row[key] for key in ('sha256','bytes','mode')} for row in inventory['source_files']}
    require(indexed == composition['source'] and len(indexed) == len(inventory['source_files']), 'selected source inventory exact census')
    methods = {method['name'] for method in census['methods']}
    require(methods == {'OnPlayerCanAcceptQuest','OnPlayerCanProgressQuestObjective','OnPlayerCanRewardQuest',
                        'OnPlayerBeforeQuestComplete','OnPlayerPassedQuestKilledMonsterCredit'}, 'five native predicate methods missing')
    require(len({row['name'] for row in census['classes']}) == len(census['classes']) == complete['direct_subclass_count'] + complete['indirect_subclass_count'],
            'selected class census count')
    for row in census['classes']:
        require(set(row['methods']) == methods and row['constructors'] and 'PlayerScript' in row['inheritance_chain'], 'selected class method/inheritance/constructor closure')
        require(all(m['kind'] in {'inherited_default','override'} for m in row['methods'].values()), 'unresolved selected handler')
    captured = {(row['sha256'],row['bytes']) for row in inputs['source_files']}
    pin, _ = census_pin()
    for descriptor in [pin] + census_sources(census):
        read_descriptor(descriptor, root)
        require((descriptor['sha256'],descriptor['bytes']) in captured, 'census source absent from input source_files: '+descriptor['path'])
