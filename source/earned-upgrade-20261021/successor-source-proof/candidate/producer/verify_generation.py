#!/usr/bin/env python3
"""Independent original native/raw oracle. Does not import or invoke generation.py."""
import argparse
import hashlib
import sys
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from ingest_capture import CaptureError, decode
from effective_data import canonical, measurement, load_inputs, validate_documents, read_descriptor

# Deliberately separate from the producer's dispatch: errors in either are observable.
ADAPTERS = {
 'quest_reward': ('quest','quest_reward'), 'creature_loot': ('creature','loot_source'),
 'gameobject_loot': ('gameobject','loot_source'), 'vendor': ('vendor','vendor_source'),
 'trainer_recipe': ('trainer','trainer_spell'), 'craft': ('recipe','recipe_product'),
 'pvp_reward': ('vendor','vendor_source'), 'dungeon_quest': ('quest','quest_reward'),
 'dungeon_drop': ('creature','loot_source'), 'disenchant': ('item','loot_source'),
 'prospecting': ('item','loot_source'), 'milling': ('item','loot_source'),
 'fishing': ('loot_template','loot_source'), 'skinning': ('creature','loot_source')}

def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def equal(actual, expected, field):
    if canonical(actual) != canonical(expected):
        raise CaptureError('independent semantic mismatch: '+field)


# The objective oracle is intentionally in this compiler-hashed file, not the producer helper.
FIVE_HOOKS = {'OnPlayerCanAcceptQuest', 'OnPlayerCanProgressQuestObjective',
              'OnPlayerCanRewardQuest', 'OnPlayerBeforeQuestComplete',
              'OnPlayerPassedQuestKilledMonsterCredit'}
EFFECT_BINDING_MISSING = ('Native source query/action token does not bind raw addon, target, '
                        'spawn, POI and fifth-hook closure or invalidate it on reload')


def _number(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and value and value.isascii() and value.lstrip('-').isdecimal():
        return int(value)
    raise CaptureError('independent original integer domain')


def objective_semantics(quest, context, tables):
    """Read original SQL domains; no producer-supported flag or semantic helper enters here."""
    missing = []; selected = []
    def need(ok, reason):
        if not ok: missing.append(reason)
    def rows(table, predicate):
        if table not in tables:
            missing.append('missing complete original table: '+table); return []
        result = []
        for ordinal, row in enumerate(tables[table]):
            if predicate(row):
                result.append(row); selected.append(dict(raw_ref=[table,ordinal,sha(row)],row=row))
        return result
    try:
        qid = quest['entry']
        raw = rows('quest_template', lambda r:_number(r['ID'])==qid)
        addon = rows('quest_template_addon', lambda r:_number(r['ID'])==qid)
        need(len(raw)==1, 'exact original quest row absent or duplicate')
        need(len(addon)==1, 'exact original addon row absent or duplicate; no default absence proof')
        if len(raw)!=1 or len(addon)!=1:return missing,selected,None
        raw,addon = raw[0],addon[0]
        n=lambda row,key:_number(row[key])
        domains=dict(raw_quest_method=n(raw,'QuestType'),raw_quest_type=n(raw,'QuestInfoID'),
            raw_main_flags=n(raw,'Flags'),authored_addon_special_flags=n(addon,'SpecialFlags'),
            loaded_cast_objective=quest['cast_objective'],loaded_exploration_or_event=quest['exploration_or_event'])
        need(domains['raw_quest_method']==2, 'unsupported quest method (auto-complete/script method)')
        need(domains['raw_quest_type']==quest['type']==0, 'raw QuestInfoID/native quest type mismatch or unsupported type')
        need(domains['raw_main_flags']==quest['flags'], 'raw main flags/native loader drift')
        need(domains['raw_main_flags'] & ~8 == 0, 'unexamined main flag semantics')
        need(domains['authored_addon_special_flags']==0, 'authored addon CAST/exploration/repeat/other unsupported special flags')
        targets=[dict(index=i,entry=n(raw,'RequiredNpcOrGo'+str(i+1)),count=n(raw,'RequiredNpcOrGoCount'+str(i+1))) for i in range(4)]
        need(canonical(quest['required_targets'])==canonical(targets), 'complete four-slot raw/loaded target mismatch')
        positive=[r for r in targets if r['entry']>0 and 0<r['count']<=65535]
        need(len(positive)==1 and all(r in positive or r['entry']==r['count']==0 for r in targets), 'mixed/negative/empty or invalid objective')
        need(quest['cast_objective'] is True and quest['exploration_or_event'] is False, 'merged CAST/exploration does not match raw ordinary loader transform')
        raw_to_loaded={'MinLevel':'minimum_level','AllowableRaces':'allowable_races','QuestLevel':'native_quest_level',
            'SuggestedGroupNum':'native_suggested_players','StartItem':'source_item','TimeAllowed':'time_allowed','RewardNextQuest':'next_quest_in_chain',
            'RequiredPlayerKills':'required_player_kills','RequiredFactionId1':'rep_objective_faction',
            'RequiredFactionValue1':'rep_objective_value','RequiredFactionId2':'rep_objective_faction2',
            'RequiredFactionValue2':'rep_objective_value2'}
        addon_to_loaded={'MaxLevel':'maximum_level','AllowableClasses':'required_classes','PrevQuestID':'previous_quest_id',
            'NextQuestID':'next_quest_id','ExclusiveGroup':'exclusive_group','BreadcrumbForQuestId':'breadcrumb_quest',
            'RewardMailTemplateID':'reward_mail_template','RequiredSkillID':'required_skill','RequiredSkillPoints':'required_skill_rank',
            'RequiredMinRepFaction':'minimum_rep_faction','RequiredMaxRepFaction':'maximum_rep_faction',
            'RequiredMinRepValue':'minimum_rep_value','RequiredMaxRepValue':'maximum_rep_value',
            'ProvidedItemCount':'source_item_count','SourceSpellID':'source_spell'}
        for source,mapping in ((raw,raw_to_loaded),(addon,addon_to_loaded)):
            for column,field in mapping.items():need(n(source,column)==quest[field], 'raw/loaded domain drift: '+column+'/'+field)
        need(n(addon,'RewardMailDelay')==0,'unsupported original reward mail delay')
        for field,prefix,countprefix,length in (('required_items','RequiredItemId','RequiredItemCount',6),('required_source_items','ItemDrop','ItemDropQuantity',4)):
            expected=[dict(index=i,item=n(raw,prefix+str(i+1)),count=n(raw,countprefix+str(i+1))) for i in range(length)]
            need(canonical(quest[field])==canonical(expected) and all(r['item']==r['count']==0 for r in expected), 'unsupported/partial raw-loaded '+field)
        for field in ('previous_quest_id','next_quest_in_chain','previous_quests','previous_chain_quests','exclusive_group',
            'exclusive_quests','breadcrumb_quest','breadcrumb_dependents','minimum_rep_faction','maximum_rep_faction',
            'minimum_rep_value','maximum_rep_value','source_spell','source_item','source_item_count','time_allowed',
            'reward_mail_template','required_skill','required_skill_rank','repeatable','daily','weekly','monthly','seasonal',
            'dungeon_finder','rep_objective_faction','rep_objective_value','rep_objective_faction2','rep_objective_value2',
            'timed','exploration_or_event','required_player_kills'):
            need(not quest[field], 'unsupported original completion/prerequisite: '+field)
        if len(positive)!=1:return missing,selected,domains
        target=positive[0]['entry']; index=positive[0]['index']
        templates=rows('creature_template',lambda r:n(r,'entry')==target)
        need(len(templates)==1,'exact positive target template missing/duplicate')
        if templates:
            t=templates[0]
            need(t['AIName']==t['ScriptName']=='','target AI/script semantics unexamined')
            need(n(t,'KillCredit1')==n(t,'KillCredit2')==0,'target KillCredit aliases unsupported')
            need(all(n(t,'difficulty_entry_'+str(i))==0 for i in range(1,4)),'difficulty target aliases unsupported')
        # The full creature table is read: unexamined aliases cannot be silently omitted.
        aliases=rows('creature_template',lambda r:n(r,'entry')!=target and target in (n(r,'KillCredit1'),n(r,'KillCredit2')))
        need(not aliases,'unexamined incoming KillCredit alias semantics')
        spawns=rows('creature',lambda r:n(r,'id')==target)
        need(bool(spawns),'positive target has no original physical spawn')
        guids={n(s,'guid') for s in spawns}; maps={n(s,'map') for s in spawns}
        for s in spawns:
            need(n(s,'phaseMask')>0 and n(s,'spawnMask')>0 and s['ScriptName']=='','target spawn phase/mask/script unsupported')
        for table,column in (('game_event_creature','guid'),('pool_creature','guid')):
            gated=rows(table,lambda r:n(r,column) in guids)
            need(not gated,'target event/pool availability unrepresented: '+table)
        for table,column,ids in (('creature_template_addon','entry',{target}),('creature_addon','guid',guids)):
            for addon_row in rows(table,lambda r:n(r,column) in ids):
                need(not addon_row['auras'] and all(n(addon_row,f)==0 for f in ('path_id','mount','emote')),'target addon aura/path/mount/emote semantics unexamined')
        smart=rows('smart_scripts',lambda r:n(r,'source_type')==0 and (n(r,'entryorguid')==target or -n(r,'entryorguid') in guids))
        need(not smart,'target SmartAI rows unexamined')
        pois=rows('quest_poi',lambda r:n(r,'QuestID')==qid)
        points=rows('quest_poi_points',lambda r:n(r,'QuestID')==qid)
        objective=[p for p in pois if n(p,'ObjectiveIndex')==index]
        need(bool(objective),'objective POI absent')
        reward_pois=[p for p in pois if n(p,'ObjectiveIndex')==-1]
        need(bool(reward_pois),'reward POI absent')
        need(all(n(p,'ObjectiveIndex') in {index,-1} for p in pois),'mixed/unsupported POI objective index')
        need(len({n(p,'id') for p in pois})==len(pois),'duplicate quest POI identity')
        need(all(any(n(p,'id')==n(point,'Idx1') for p in pois) for point in points),'orphan quest POI point')
        for reward_poi in reward_pois:
            need(n(reward_poi,'MapID') in maps and bool([r for r in points if n(r,'Idx1')==n(reward_poi,'id')]),'reward POI map/points closure missing')
        for role,branches in (('objective',objective),('reward',reward_pois)):
            need(any(n(p,'MapID')==g['map_id']==n(s,'map') and n(s,'phaseMask') & g['phase_mask'] and n(s,'spawnMask') & g['spawn_mask']
                for p in branches for g in quest['giver_spawns'] for s in spawns), role+' POI has no compatible giver/target route')
            for p in branches:
                need(n(p,'MapID') in maps,role+' POI/target map mismatch')
                pp=[r for r in points if n(r,'Idx1')==n(p,'id')]
                need(bool(pp),role+' POI points absent')
                need(len({n(r,'Idx2') for r in pp})==len(pp),'duplicate POI point ordinal')
                for point in pp:n(point,'X');n(point,'Y')
        giver_maps={g['map_id'] for g in quest['giver_spawns']}
        need(bool(giver_maps & maps & {n(p,'MapID') for p in objective}),'giver/target/objective have no common map')
        need(any(s['phaseMask'] & g['phase_mask'] and s['spawnMask'] & g['spawn_mask'] for s in spawns for g in quest['giver_spawns'] if n(s,'map')==g['map_id']), 'giver/target phase or spawn mask has no intersection')
        for r in rows('spawn_group',lambda r:n(r,'spawnType')==0 and n(r,'spawnId') in guids):
            groups=rows('spawn_group_template',lambda g:n(g,'groupId')==n(r,'groupId'))
            need(len(groups)==1 and n(groups[0],'groupFlags') & ~1==0,'target spawn group semantics unexamined')
        return missing,selected,domains
    except (KeyError,TypeError,ValueError,IndexError) as error:
        missing.append('missing/invalid original objective field: '+str(error));return missing,selected,None


def _proof_document(descriptor, root, snapshot):
    if not isinstance(descriptor,dict) or descriptor.get('generation')!=snapshot:
        raise CaptureError('independent proof descriptor generation')
    return decode(read_descriptor(descriptor,Path(root)))


def verify_ordinary_proof(proof, quest, context, root, binding=None, catalogue_descriptor=None, executable=False):
    """Authenticate originals, recompute semantics, and keep static and effect custody separate."""
    if proof.get('schema')!='ordinary-positive-creature-target-proof-v1':raise CaptureError('independent objective proof schema')
    if proof.get('static_status') not in {'PROVEN','UNKNOWN'}:raise CaptureError('independent objective static status')
    if not isinstance(proof.get('missing'),list) or any(not isinstance(r,str) or not r for r in proof['missing']):raise CaptureError('independent objective missing reasons')
    if proof['static_status']=='UNKNOWN' and not proof['missing']:raise CaptureError('independent objective UNKNOWN reason missing')
    snapshot=proof['input_manifest']['generation']
    inputs=_proof_document(proof['input_manifest'],root,snapshot)
    input_root=Path(proof['input_manifest']['path']).parent
    if inputs.get('schema')!='core1-current-acquisition-input-v1':raise CaptureError('independent objective original input schema')
    equal(sha(inputs['tables']),inputs['binding']['world_dump_digest'],'objective original raw descriptor generation')
    if binding is not None:equal(inputs['binding'],binding,'objective original input binding')
    if catalogue_descriptor is not None:equal(inputs['catalog'],catalogue_descriptor,'objective original catalogue descriptor')
    catalog=_proof_document(inputs['catalog'],input_root,snapshot)
    attestation=_proof_document(inputs['attestation'],input_root,snapshot)
    original=[q for q in catalog['quests'] if q['entry']==quest['entry']]
    equal(original,[quest],'objective original loaded quest')
    equal(proof['quest_entry'],quest['entry'],'objective quest identity')
    equal(proof['original_catalogue_sha256'],inputs['catalog']['sha256'],'objective catalogue bytes')
    equal(proof['observation_generation'],catalog['observation_generation'],'objective observation generation')
    equal(proof['phase'],catalog['phase'],'objective phase')
    equal(context['phase'],catalog['phase'],'objective actor/source phase')
    source_digest=sha(inputs['source_files'])
    equal(proof['source_files_digest'],source_digest,'objective source generation')
    equal(attestation['source_files_digest'],source_digest,'objective attested sources')
    equal(attestation['exporter_revision'],source_digest,'objective exporter generation')
    equal(attestation['catalog_sha256'],inputs['catalog']['sha256'],'objective attested catalogue')
    equal(attestation['binding_sha256'],sha(inputs['binding']),'objective attested binding')
    config=read_descriptor(inputs['config'],input_root)
    equal(hashlib.sha256(config).hexdigest(),inputs['binding']['config_digest'],'objective original config bytes')
    equal(attestation['config_sha256'],inputs['binding']['config_digest'],'objective attested config')
    equal(attestation['observation_generation'],catalog['observation_generation'],'objective attested observation')
    equal(attestation['phase'],catalog['phase'],'objective attested phase')
    source_bytes={}
    for d in inputs['source_files']:
        source_bytes[(d['sha256'],d['bytes'])]=read_descriptor(d,input_root)
    # Authenticate complete table originals, including zero-row exclusion tables.
    tables={}
    for table,d in proof['table_descriptors'].items():
        equal(d,inputs['tables'].get(table),'objective original table descriptor '+table)
        raw=read_descriptor(d,input_root)
        values=[decode(line) for line in raw.splitlines() if line]
        equal(len(values),d['rows'],'objective original table row count '+table)
        if any(set(r)!=set(d['columns']) for r in values):raise CaptureError('independent objective original table columns')
        tables[table]=values
    missing,selected,domains=objective_semantics(quest,context,tables)
    for record in proof['raw_rows']:
        table,ordinal,checksum=record['raw_ref']
        if type(ordinal) is not int or ordinal<0:raise CaptureError('independent objective raw ordinal')
        row=tables[table][ordinal]
        equal([sha(row),row],[checksum,record['row']],'objective retained raw original')
    refs={canonical(r['raw_ref']) for r in proof['raw_rows']}
    if len(refs)!=len(proof['raw_rows']):raise CaptureError('independent objective duplicate raw witness')
    if not {canonical(r['raw_ref']) for r in selected}<=refs:raise CaptureError('independent objective required raw join omitted')
    equal(proof['loaded_targets'],quest['required_targets'],'objective retained true target vector')
    if domains is not None:
        if 'flag_domains' in proof:equal(proof['flag_domains'],domains,'objective distinct flag domains')
        else:missing.append('retained distinct raw/loaded flag domains missing')
    positive=[r for r in quest['required_targets'] if r['entry']>0 and r['count']>0]
    if len(positive)==1:
        if 'target_entry' in proof and 'target_index' in proof:
            equal(proof['target_entry'],positive[0]['entry'],'objective retained target identity')
            equal(proof['target_index'],positive[0]['index'],'objective retained target index')
        else:missing.append('retained target identity/index missing')
    if proof.get('census') is None:missing.append('retained five-hook selected source census missing')
    else:
        if (proof['census']['sha256'],proof['census']['bytes']) not in source_bytes:raise CaptureError('independent census descriptor outside captured original source generation')
        census=_proof_document(proof['census'],input_root,snapshot)
        equal(census['bindings']['complete_source_identity'],inputs['binding']['core_revision'],'objective selected native source identity')
        missing.extend(verify_census(census,source_bytes,input_root))
    if proof['static_status'] not in {'PROVEN','UNKNOWN'}:raise CaptureError('independent objective static status')
    if proof['static_status']=='PROVEN' and missing:raise CaptureError('independent fabricated objective support: '+'; '.join(missing))
    if proof['static_status']=='PROVEN':equal(proof['missing'],[],'objective proved missing census')
    # Current bindings never fingerprint these fields; a JSON assertion cannot repair that.
    if proof.get('effect_custody',{}).get('status')!='UNKNOWN':raise CaptureError('independent fabricated native effect custody')
    if not proof['effect_custody'].get('missing_native_binding'):raise CaptureError('independent missing native binding reason')
    if executable:raise CaptureError('independent native effect custody UNKNOWN: '+EFFECT_BINDING_MISSING)
    if proof['static_status']=='UNKNOWN':missing.extend('producer retained uncertainty: '+reason for reason in proof['missing'])
    return dict(static_status='UNKNOWN' if missing else 'PROVEN',missing=missing,effect_custody='UNKNOWN',missing_native_binding=EFFECT_BINDING_MISSING)


def verify_census(census, captured_sources, root):
    """Re-read complete selected composition and independently enumerate derived classes."""
    import re
    if census.get('schema')!='core1-selected-player-script-five-hook-census-v1':raise CaptureError('independent five-hook census schema')
    bindings=census['bindings']
    def document(pin):
        p=Path(pin['path']);raw=read_descriptor(dict(pin,bytes=p.stat().st_size),root)
        if (pin['sha256'],len(raw)) not in captured_sources:raise CaptureError('independent census original source capture missing')
        return decode(raw)
    composition=document(bindings['composition']);inventory=document(bindings['selected_source_inventory'])
    equal(sha(composition['source']),bindings['complete_source_identity'],'census complete selected source identity')
    equal(bindings['complete_source_identity'],composition['complete_source_identity'],'census composition identity')
    expected=composition['source'];actual={d['logical_path']:d for d in inventory['source_files']}
    equal(sorted(actual),sorted(expected),'census complete source inventory')
    audited_sources = {
        'src/server/game/Globals/ObjectMgr.cpp':'35ee4b42090492f2771ada50c918f3a894323b3edf9bde8c8bee27ff1f8de0df',
        'src/server/game/Quests/QuestDef.cpp':'6aff64eb450a7b44a787eef00099c1b553552df339865ac913529715ca7f84c4',
        'src/server/game/Quests/QuestDef.h':'baaee3502a85ce4e02e045ae67efb1f48b7c6f6aad162f4b116496f028390b20',
        'src/server/game/Entities/Player/KillRewarder.cpp':'cceb64c864c677d75fbbbefce5e2184090432f8e9fffcf31f9f9796b3efcc9d2',
        'src/server/game/Entities/Player/PlayerQuest.cpp':'df787559c12084a0a838672371595daac575a1469cb0b106bdce82798d93b50d',
        'modules/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgAction.cpp':'aacacd683a6b397bdc32fb25d42619b953d8e418e383519c10b35e5beb19f716',
        'modules/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgBaseAction.cpp':'8d969dd30f179b83272b9903a41e0e6b9a140f256d282cdcee44f909e8032563',
        'modules/mod-playerbots/src/Ai/Base/Value/GrindTargetValue.cpp':'4c633b727e2d413469f9f40affb19043dd8c5d17210ed3aec57d7faddc354292',
        'modules/mod-python-engine/src/Scripts/PlayerScript.cpp':'52356448e75406e9b76918846748ddd095a3f6689d454d26c3a85dfb1418c471',
        'modules/mod-progression-system/src/SharedPhaseGate.cpp':'10ff3e7a476260a5e85a24419dd42332f2f8f5bed4b176fb4de8137860899728',
        'modules/mod-playerbots/src/Guide/PythonPopulationBridge.cpp':'4454565b58c79a363440a18c409e03f7cb51cac7421e1ce3e745dae142870631',
        'modules/mod-python-engine/src/Hooks/HookMacros.h':'fb45ca6f7bec907c1043b2ce04a9eda954e1731ab966fffbf241a8ff43a95627',
        'src/server/game/Scripting/ScriptDefines/PlayerScript.h':'54e96ac9cc3890c1cc359274581771f22afd1f1738c1557b6fba003dea525cf7',
        'src/server/game/Scripting/ScriptDefines/PlayerScript.cpp':'12d3f8d2d4f13103621ee00354d8d31b93253d9c07828d4d930a9ee8fb9048f2'}
    # These are audited implementation bytes, never quest/item/target allowlists.
    source_missing=['unexamined native implementation: '+name for name,checksum in audited_sources.items() if name not in actual or actual[name]['sha256']!=checksum]
    source={};parsed={};registration_counts=Counter()
    def stripped(text):
        return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',lambda m:'\n'*m.group().count('\n')+' '*(len(m.group())-m.group().count('\n')),text,flags=re.S)
    for logical,d in actual.items():
        equal([d['sha256'],d['bytes']],[expected[logical]['sha256'],expected[logical]['bytes']],'census selected bytes '+logical)
        wanted=composition['source_overrides'].get(logical,str(Path(composition['virtual_source_root'])/logical))
        equal(str(Path(d['path'])),wanted,'census selected physical source '+logical)
        raw=read_descriptor(d,Path(root));source[logical]=raw
        if Path(logical).suffix not in {'.cpp','.cc','.cxx','.h','.hpp','.hxx','.inc'}:continue
        text=stripped(raw.decode('utf-8',errors='surrogateescape'))
        registration_counts.update(re.findall(r'\bnew\s+(\w+)\s*\(',text))
        for match in re.finditer(r'\b(?:class|struct)\s+(\w+)\s*(?:final\s*)?(?::\s*([^;{}]+))?\s*\{',text):
            name,bases=match.group(1),match.group(2) or ''
            base_names=[re.sub(r'\b(public|private|protected|virtual)\b','',b).strip().split('::')[-1] for b in bases.split(',') if b.strip()]
            start=match.end();depth=1;end=start
            while depth and end<len(text):
                depth+=(text[end]=='{')-(text[end]=='}');end+=1
            if depth:text_body='__INDEPENDENT_INCOMPLETE__'
            else:text_body=text[start:end-1]
            if name not in parsed:parsed[name]=[]
            parsed[name].append((base_names,text_body,logical))
    generated=composition['generated'];generated_actual={d['logical_path']:d for d in inventory['generated_files']}
    equal(sorted(generated_actual),sorted(generated),'census generated loader inventory')
    for logical,d in generated_actual.items():
        equal([d['sha256'],d['bytes']],[generated[logical]['sha256'],generated[logical]['bytes']],'census generated bytes')
        equal(d['path'],composition['generated_overrides'].get(logical,str(Path(composition['virtual_generated_root'])/logical)),'census selected generated file')
        read_descriptor(d,Path(root))
    derived={'PlayerScript'}
    while True:
        added={name for name,definitions in parsed.items() if any(set(bases)&derived for bases,_,_ in definitions)}-derived
        if not added:break
        derived|=added
    derived.remove('PlayerScript')
    equal(sorted(derived),sorted(c['name'] for c in census['classes']),'census independent inheritance closure')
    def reference(ref):
        raw=source.get(ref['logical_path'])
        if raw is None:raise CaptureError('independent census reference outside selected composition')
        equal([hashlib.sha256(raw).hexdigest(),len(raw)],[ref['sha256'],ref['bytes']],'census reference original bytes')
        equal(ref['path'],actual[ref['logical_path']]['path'],'census source range physical association')
        lines=raw.decode('utf-8',errors='surrogateescape').splitlines()
        start,end=ref['start_line'],ref['end_line']
        if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):raise CaptureError('independent census source line range')
        text='\n'.join(lines[start-1:end])
        if 'text' in ref:
            compact=lambda v:re.sub(r'\s+','',re.sub(r'\[\[.*?\]\]','',v))
            if compact(ref['text']) not in compact(text):raise CaptureError('independent census source range text drift')
        return stripped(text)
    methods={m['name']:m for m in census['methods']}
    equal(sorted(methods),sorted(FIVE_HOOKS),'census required five methods')
    for name,m in methods.items():
        default=reference(m['default']);dispatch=reference(m['dispatch'])
        if not re.search(r'\b'+name+r'\s*\([^;{}]*\)\s*\{\s*return\s+true\s*;\s*\}',default):raise CaptureError('independent census default semantics')
        if name not in dispatch or 'CALL_ENABLED_BOOLEAN_HOOKS' not in dispatch:raise CaptureError('independent census selected dispatch')
    missing=source_missing+list(census['unresolved'])
    classes={c['name']:c for c in census['classes']}
    def owner(name,method,seen=()):
        if name=='PlayerScript':return 'PlayerScript'
        if name in seen:raise CaptureError('independent census inheritance cycle')
        defs=parsed[name]
        if len(defs)!=1:raise CaptureError('independent ambiguous selected class definition '+name)
        bases,body,_=defs[0]
        if body=='__INDEPENDENT_INCOMPLETE__':raise CaptureError('independent selected PlayerScript class body incomplete')
        if re.search(r'\b'+method+r'\s*\(',body):return name
        parents=[b for b in bases if b in derived or b=='PlayerScript']
        if len(parents)!=1:raise CaptureError('independent unresolved selected inheritance '+name)
        return owner(parents[0],method,seen+(name,))
    for name,c in classes.items():
        reference(c['definition'])
        for constructor in c['constructors']:
            reference(constructor['reference'])
            raw=source[constructor['reference']['logical_path']].decode('utf-8').splitlines()
            text='\n'.join(raw[constructor['reference']['start_line']-1:constructor['reference']['end_line']])
            names=re.findall(r'PlayerScript\s*\(\s*"([^"]+)"',text)
            if constructor['script_name'] not in names:raise CaptureError('independent census literal constructor name')
            hooks=sorted(set(re.findall(r'\bPLAYERHOOK_\w+',text)))
            equal(sorted(constructor['enabled_hooks']),hooks or ['ALL'],'census independent constructor hook enablement')
        if not c['constructors']:missing.append('constructor enablement missing: '+name)
        for ref in c.get('registration_sites',[]):reference(ref)
        equal(len(c.get('registration_sites',[])),registration_counts[name],'census independent registration-site count '+name)
        if 'hook_enablement' in c:
            for hook_name,method_record in methods.items():
                enabled=any('ALL' in ctor['enabled_hooks'] or method_record['hook_id'] in ctor['enabled_hooks'] for ctor in c['constructors'])
                equal(c['hook_enablement'][hook_name],enabled,'census independent enabled method')
        for method in FIVE_HOOKS:
            record=c['methods'][method];actual_owner=owner(name,method)
            equal(record['owner'],actual_owner,'census independent method owner '+name+'.'+method)
            equal(record['kind'],'inherited_default' if actual_owner=='PlayerScript' else 'override','census independent method kind')
            body=reference(record['reference'])
            expected_path='src/server/game/Scripting/ScriptDefines/PlayerScript.h' if actual_owner=='PlayerScript' else parsed[actual_owner][0][2]
            equal(record['reference']['logical_path'],expected_path,'census independent method source association')
            if method not in body:raise CaptureError('independent census method body reference missing')
            if actual_owner!='PlayerScript' and actual_owner not in {'PythonPlayerScript','progression_system_shared_phase_gate'}:
                missing.append('unexamined selected override: '+actual_owner+'.'+method)
            if method=='OnPlayerPassedQuestKilledMonsterCredit' and actual_owner=='PythonPlayerScript' and 'PASSED_QUEST_KILLED_MONSTER_CREDIT' not in body:
                missing.append('fifth Python override body unexamined')
    # No claim that this source census establishes loaded registration or passive Python registry state.
    if census.get('runtime_binding',{}).get('status')!='UNKNOWN':raise CaptureError('independent census fabricated runtime binding')
    return missing


def verify_retained_objectives(path, expected, manifest, native, catalog):
    """Find proof receipts only through their retained original byte descriptors."""
    root=Path(path).parent;custody_path=root/'custody.json'
    executable={f['key']:f for f in native['acquisitions']}
    relevant={s['key']:s for s in native['slot_custody']['slots'] if s['native_quest'].get('cast_objective') is True}
    if not custody_path.exists():
        if any(k in executable for k in relevant):raise CaptureError('independent ordinary objective original custody missing')
        return []
    custody=decode(custody_path.read_bytes())
    equal(custody['manifest_sha256'],expected,'objective sealed custody manifest')
    equal(custody['input_generation'],sha(manifest['binding']),'objective sealed custody generation')
    results=[];seen=set()
    declared_receipts={row['reference']['document'].removeprefix('receipt:') for row in custody['semantic_proofs'] if row['reference']['document'].startswith('receipt:')}
    for descriptor in custody['descriptor_custody']:
        raw=read_descriptor(descriptor,root)
        if not raw.lstrip().startswith(b'{'):continue
        try:document=decode(raw)
        except CaptureError:
            if descriptor['sha256'] in declared_receipts:raise
            continue
        if not isinstance(document,dict) or not isinstance(document.get('data'),dict):continue
        proof=document['data'].get('source_proofs',{}).get('ordinary_positive_target')
        if proof is None:continue
        equal(document['source_revision'],native['exporter_revision'],'objective receipt source revision')
        equal(document['snapshot_id'],manifest['binding']['snapshot_id'],'objective receipt snapshot')
        qid=proof['quest_entry']
        for key,values in document['data'].get('slots',{}).items():
            if key.startswith('quest:'+str(qid)+':') and set(values)&{'condition','group_size','capabilities','unresolved'}:
                raise CaptureError('independent source support supplied without native effect custody')
        if qid in seen:raise CaptureError('independent duplicate ordinary objective proof')
        seen.add(qid)
        quests=[q for q in catalog['quests'] if q['entry']==qid]
        if len(quests)!=1:raise CaptureError('independent ordinary proof original quest missing')
        context=dict(catalog,phase=catalog['phase'])
        is_executable=any(s['source_entry']==qid and k in executable for k,s in relevant.items())
        results.append(dict(quest_entry=qid,**verify_ordinary_proof(proof,quests[0],context,root,
            manifest['binding'],native['slot_custody']['catalogue'],is_executable)))
    for key,slot in relevant.items():
        if key in executable and slot['source_entry'] not in seen:raise CaptureError('independent ordinary objective proof missing for executable source')
    return results


def verify(path, expected, documents, evidence):
    m, tables, policy, native = load_inputs(path, expected)
    validate_documents(documents)
    graph, manifest, projections = (documents[k] for k in ('graph','manifest','projection'))
    b=m['binding']; gd=sha(graph)
    original_slots = {slot['key']: slot for slot in native['slot_custody']['slots']}
    # Original native catalogue custody is checked here independently of generation dispatch.
    original_raw = read_descriptor(native['slot_custody']['catalogue'], Path(path).parent)
    original_catalog = decode(original_raw)
    objective_checks = verify_retained_objectives(path, expected, m, native, original_catalog)
    independent_slots = {}
    for quest in original_catalog['quests']:
        for reward in quest['rewards']:
            key = 'quest:%s:%s:%s' % (quest['entry'], reward['kind'], reward['index'])
            if key in independent_slots:
                raise CaptureError('independent duplicate native slot')
            independent_slots[key] = (quest['entry'],reward['item'],reward['count'],reward['kind'],reward['index'])
    equal(len(independent_slots),original_catalog['reward_count'],'original reward count')
    equal(len(original_catalog['items']),original_catalog['item_count'],'original item count')
    equal(len(original_catalog['quests']),original_catalog['quest_count'],'original quest count')
    equal(sorted(independent_slots),sorted(original_slots),'original native slot census')
    for key, slot in original_slots.items():
        equal([slot['source_entry'],slot['item_entry'],slot['minimum_count'],slot['native_reward']['kind'],slot['native_reward']['index']],list(independent_slots[key]),'original slot identity '+key)
    equal(sorted(row['entry'] for row in native['items']),sorted(row['entry'] for row in original_catalog['items']),'original native item identities')
    for fact in native['acquisitions']:
        if fact['family'] in {'quest_reward','dungeon_quest'}:
            qid,item_id,count,kind,index=independent_slots[fact['key']]
            suffix=str(index+1);column=('RewardItem' if kind=='fixed' else 'RewardChoiceItemID')+suffix
            quantity=('RewardAmount' if kind=='fixed' else 'RewardChoiceItemQuantity')+suffix
            witnessed=False
            for table,ordinal,row_digest in fact['raw_refs']:
                row=tables[table][ordinal]
                if sha(row)!=row_digest:raise CaptureError('independent raw source custody')
                def native_number(x):
                    return int(x) if isinstance(x,str) and x.isascii() and x.isdecimal() else x
                if table=='quest_template' and native_number(row.get('ID'))==qid and native_number(row.get(column))==item_id and native_number(row.get(quantity))==count:
                    witnessed=True
            if not witnessed:raise CaptureError('independent qualified original SQL slot/count')
    condition_ids=[c['condition_id'] for c in graph['condition_roots']]
    if len(condition_ids)!=len(set(condition_ids)):
        raise CaptureError('duplicate_condition')
    equal(condition_ids,sorted(condition_ids),'canonical condition root order')
    equal([e['edge_id'] for e in graph['edges']],sorted(e['edge_id'] for e in graph['edges']),'canonical graph edge order')
    for collection,nodes in graph['nodes'].items():
        equal([n['id'] for n in nodes],sorted(n['id'] for n in nodes),'canonical node order: '+collection)
    equal([p['item_id'] for p in projections],sorted(p['item_id'] for p in projections),'canonical projection item order')
    for p in projections:
        equal([s['source_id'] for s in p['sources']],sorted(s['source_id'] for s in p['sources']),'canonical projection source order')
    for k in ('core_revision','playerbots_revision','progression_revision','content_revision','world_dump_digest','dbc_set_digest'):
        equal(graph[k],b[k],'graph.'+k)
    equal(manifest['source'],{k:b[k] for k in ('candidate_id','core_revision','playerbots_revision','progression_revision','world_dump_digest','config_digest','dbc_set_digest')},'manifest.source')
    for k in ('graph_digest','reproducible_peer_digest'):
        equal(manifest[k],gd,'manifest.'+k)
    equal(manifest['compiler_revision'],b['compiler_revision'],'compiler revision')
    equal([manifest['started_at'],manifest['completed_at']],[b['captured_at']]*2,'capture time')
    equal(manifest['row_counts'],{t:len(rows) for t,rows in tables.items()},'raw row counts')
    equal(manifest['migrations'],[dict(ordinal=d['ordinal'],name=d['name'],digest=d['sha256']) for d in m['migrations']],'migration order')
    equal(manifest['dbc_files'],sorted([dict(name=d['name'],digest=d['sha256']) for d in m['dbc_files']],key=lambda d:d['name']),'DBC custody')
    equal(evidence['stages'],policy['stages'],'all37 policy identities/completeness')
    stage_counts={}
    for stage in policy['stages']:
        counts=Counter(f['family'] for f in native['acquisitions'] if stage['id'] in f['stages'])
        stage_counts[stage['id']]={family:counts[family] for family in sorted(native['family_dispositions'])} if stage['input_complete'] else None
    equal(evidence['stage_source_counts'],stage_counts,'per-stage/family source census')
    equal(evidence['raw_row_dispositions'],native['dispositions'],'raw omissions')
    equal(evidence['family_dispositions'],native['family_dispositions'],'family dispositions')
    equal(evidence['input_generation'],sha(b),'generation identity')
    equal(evidence['native_input_digest'],sha(native),'native custody')
    equal(evidence['consumer_generation'],sha(dict(source=manifest['source'],compiler_revision=manifest['compiler_revision'],migrations=manifest['migrations'],dbc_files=manifest['dbc_files'],content_revision=graph['content_revision'])),'selected consumer generation')
    equal(evidence['loaded_generation_verified'],native['loaded_generation_verified'],'loaded generation')
    equal(evidence['effective_dataset_accepted'],False,'acceptance authority')
    equal(evidence['fullcurrent_complete'],False,'fullcurrent authority')
    equal(evidence['full14_complete'],False,'full14 authority')
    equal(evidence['full37_complete'],False,'full37 acceptance authority')
    equal(evidence['measurements'],{k:measurement(v) for k,v in documents.items()},'document measurements')
    expected_nodes=defaultdict(dict)
    for item in native['items']:
        expected_nodes['item'][item['entry']]=item['attributes']
    expected_sources={}; expected_edges={}; conditions={}; item_sources=Counter()
    item_condition_ids={}
    associated_items={s['item_entry'] for s in native['slot_custody']['slots'] if s['native_quest'].get('source_rule_version')=='core1-ordinary-quest-source-rules-v1'}
    for identity in associated_items:
        original=expected_nodes['item'][identity]
        definitions=[dict(op='leaf',condition_type=15,values=[original['allowable_class'],0,0]),dict(op='leaf',condition_type=16,values=[original['allowable_race'],0,0])]
        if original['required_level']!=0:definitions.append(dict(op='leaf',condition_type=27,values=[original['required_level'],3,0]))
        if original['required_skill']!=0:definitions.append(dict(op='indeterminate'))  # No effective/base skill equivalence is asserted for items.
        definition=dict(op='all',arguments=sorted(definitions,key=canonical))
        root='condition:'+sha(definition);conditions[root]=definition;item_condition_ids[identity]=root
    for fact in native['acquisitions']:
        if fact['family'] not in ADAPTERS:
            if not any(x.get('source')==fact['key'] and x.get('native')==fact for x in evidence['unresolved'] if isinstance(x,dict)):
                raise CaptureError('unsupported native family not retained')
            continue
        kind,edge_kind=ADAPTERS[fact['family']]
        expected_nodes[kind].setdefault(fact['source_entry'],{})
        eid='source:'+sha(fact['key']); expected_sources[eid]=fact; item_sources[fact['item_entry']]+=1
        current = fact['stages']==['S01'] and policy['stages'][0]['name']=='1_19' and [s['id'] for s in policy['stages'] if s['loaded']]==['S01']
        original=fact['condition']
        if fact['unresolved'] or len(fact['variant']) or not current:
            original=dict(op='indeterminate') if original is None else dict(op='all',arguments=[original,dict(op='indeterminate')])
        condition=None
        if original is not None:
            condition='condition:'+sha(original); conditions[condition]=original
        av=dict(minimum_frontier=fact['frontier'][0],maximum_frontier=fact['frontier'][1],phase_mask=fact['phase_mask'],spawn_mask=fact['spawn_mask'],event_ids=sorted(fact['event_ids']),pool_ids=sorted(fact['pool_ids']),condition_id=condition)
        expected_edges[eid]=(fact,kind,edge_kind,av)
        reagents=Counter()
        for item,count,*_ in fact['reagents']:
            reagents[item]+=count
        for item,count in reagents.items():
            expected_edges[eid+'.reagent.'+str(item)]=(fact,kind,'recipe_reagent',av,item,count)
    actual_edges={e['edge_id']:e for e in graph['edges']}
    if len(actual_edges)!=len(graph['edges']):
        raise CaptureError('duplicate graph edge')
    equal(sorted(actual_edges),sorted(expected_edges),'source/reagent edge census')
    for eid,entry in expected_edges.items():
        f,kind,edge_kind,av=entry[:4]; e=actual_edges[eid]
        equal(e['from'],dict(kind=kind,id=f['source_entry']),eid+'.native source')
        equal(e['availability'],av,eid+'.phase/condition')
        equal(e['kind'],edge_kind,eid+'.edge family')
        reagent=len(entry)>4
        equal(e['to'],dict(kind='item',id=entry[4] if reagent else f['item_entry']),eid+'.native item')
        equal(e['semantics'],'recipe_reagent' if reagent else f['family'],eid+'.family')
        expected_attributes = dict(count=entry[5],native_source_key=f['key']) if reagent else dict(native_acquisition=f,generation=sha(b))
        natural_key = [f['source_entry'],entry[4] if reagent else f['item_entry']]
        if not reagent and f['family'] in {'quest_reward', 'dungeon_quest'}:
            original_slot = original_slots[f['key']]; reward = original_slot['native_reward']
            category = {'fixed':'guaranteed','choice':'choice'}[reward['kind']]
            suffix = reward['index'] + 1
            natural_key.extend([category, suffix])
            expected_attributes.update(native_custody=original_slot,native_table='quest_template',quest_id=f['source_entry'],
                reward_kind=category,native_reward_index=reward['index'],sql_reward_column_suffix=suffix,
                native_item_column=('RewardItem' if category=='guaranteed' else 'RewardChoiceItemID')+str(suffix),
                minimum_count=reward['count'],maximum_count=reward['count'])
        equal(e['attributes'],expected_attributes,eid+'.native facts')
        equal(e['provenance'],dict(source='normalized.recipe_reagent' if reagent else 'normalized.'+f['family'],natural_key=natural_key),eid+'.natural key')
    collections={'quest':'quests','item':'items','creature':'creatures','gameobject':'gameobjects','spell':'spells','trainer':'trainers','vendor':'vendors','taxi_node':'taxi_nodes','area':'areas','map':'maps','dungeon':'dungeons','loot_template':'loot_templates','recipe':'recipes'}
    for kind,collection in collections.items():
        actual={n['id']:n for n in graph['nodes'][collection]}
        equal(sorted(actual),sorted(expected_nodes[kind]),collection+'.node census')
        if len(actual)!=len(graph['nodes'][collection]):
            raise CaptureError('duplicate native node')
        for item,node in actual.items():
            equal(node['attributes'],expected_nodes[kind][item],collection+'.native attributes')
            equal(node['availability'],dict(minimum_frontier=1,maximum_frontier=80,phase_mask=1,spawn_mask=1,event_ids=[],pool_ids=[],condition_id=item_condition_ids.get(item) if kind=='item' else None),collection+'.availability')
    # Native normalizes every commutative expression before its digest; require that identity.
    def ast(x):
        if x['op'] in ('all','any'):
            return dict(op=x['op'],arguments=sorted([ast(a) for a in x['arguments']],key=canonical))
        if x['op']=='not':
            return dict(op='not',argument=ast(x['argument']))
        return x
    for c in graph['condition_roots']:
        equal(c['expression'],ast(c['expression']),'canonical condition argument order')
    equal({c['condition_id']:ast(c['expression']) for c in graph['condition_roots']},{k:ast(v) for k,v in conditions.items()},'condition semantic census')
    projected={p['item_id']:p for p in projections}
    equal(sorted(projected),sorted(expected_nodes['item']),'effective item projection census')
    if len(projected)!=len(projections):
        raise CaptureError('duplicate item projection')
    seen=set()
    for item,p in projected.items():
        equal(p['content_revision'],b['content_revision'],'projection revision')
        equal(p['graph_digest'],gd,'projection graph digest')
        for s in p['sources']:
            eid=s['source_id']
            if eid in seen or eid not in expected_sources:
                raise CaptureError('duplicate/unexpected native projection source')
            seen.add(eid);f=expected_sources[eid]
            equal(item,f['item_entry'],eid+'.projection item')
            equal(s['source_kind'],f['family'],eid+'.projection family')
            equal(s['source_ref'],dict(kind=ADAPTERS[f['family']][0],id=f['source_entry']),eid+'.projection source')
            prob=Fraction(*f['probability'])
            equal([s['chance_numerator'],s['chance_denominator']],[prob.numerator,prob.denominator],eid+'.native probability')
            for out,field in {'minimum_count':'minimum_count','maximum_count':'maximum_count','required_group_size':'group_size','money_cost_copper':'copper','travel_seconds':'travel','seconds_per_attempt':'attempt','risk_milli':'risk','contention_milli':'contention','quest_alternative_group':'alternative'}.items():
                equal(s[out],f[field],eid+'.'+field)
            equal([s['minimum_phase'],s['maximum_phase']],f['frontier'],eid+'.phase interval')
            equal(s['condition_root'],expected_edges[eid][3]['condition_id'],eid+'.condition root')
            equal(s['required_capabilities'],sorted(f['capabilities']),eid+'.capabilities')
            equal([s['required_profession_skill_id'],s['required_profession_skill_value']],f['profession'],eid+'.profession')
            reagents=Counter(); eligibility={}
            for identity,count,available,phase in f['reagents']:
                reagents[identity]+=count
                previous=eligibility.get(identity,(True,True))
                eligibility[identity]=(previous[0] and available,previous[1] and phase)
            equal(s['craft_dependencies'],sorted([dict(item_id=i,count=c,available=eligibility[i][0],phase_authorized=eligibility[i][1],earned=True) for i,c in reagents.items()],key=canonical),eid+'.native reagents')
    equal(sorted(seen),sorted(expected_sources),'native source census')
    equal(evidence['item_dispositions'],[dict(item_id=i['entry'],sources=item_sources[i['entry']],reason=i['no_source_reason']) for i in sorted(native['items'],key=lambda i:i['entry'])],'effective item omissions')
    unresolved=list(native['unresolved'])
    expected_incompatible=[]; reverse=Counter(); provenance=Counter(); reagent_graph=defaultdict(set)
    for f in sorted(native['acquisitions'],key=lambda x:x['key']):
        key=f['key']
        if f['family'] not in ADAPTERS:
            unresolved.append(dict(source=key,reason='unsupported_family',native=f));continue
        kind,ek=ADAPTERS[f['family']]
        qualified = [f['source_entry'], f['item_entry']]
        if f['family'] in {'quest_reward','dungeon_quest'}:
            reward = original_slots[key]['native_reward']
            qualified += ['guaranteed' if reward['kind']=='fixed' else 'choice',reward['index']+1]
        reverse[(ek,kind,*qualified,f['family'])]+=1
        provenance[('normalized.'+f['family'],*qualified)]+=1
        if [f['minimum_count'],f['maximum_count']] != [1,1]:
            expected_incompatible.append(dict(source=key,field='minimum_count/maximum_count',actual=[f['minimum_count'],f['maximum_count']],consumer_error='unsupported_nonunit_yield'))
        if f['stages']!=['S01'] or policy['stages'][0]['name']!='1_19' or [s['id'] for s in policy['stages'] if s['loaded']]!=['S01']:
            expected_incompatible.append(dict(source=key,field='stages',actual=f['stages'],consumer_error='no_native_stage_identity_field'))
        for field in sorted(f['variant']):
            expected_incompatible.append(dict(source=key,field='variant.'+field,actual=f['variant'][field],consumer_error='no_native_variant_predicate_field'))
        if Fraction(*f['probability'])<1 and (f['family'] not in {'creature_loot','gameobject_loot','dungeon_drop','disenchant','prospecting','milling','fishing','skinning'} or f['attempt']<=0):
            p=Fraction(*f['probability'])
            expected_incompatible.append(dict(source=key,field='probability/source_kind/seconds_per_attempt',actual=[p.numerator,p.denominator,f['family'],f['attempt']],consumer_error='invalid_source_data'))
        if f['family']=='craft' and (not f['reagents'] or f['profession'][0]==0):
            expected_incompatible.append(dict(source=key,field='craft_dependencies/required_profession_skill_id',actual=[len({x[0] for x in f['reagents']}),f['profession'][0]],consumer_error='invalid_source_data'))
        for identity in {x[0] for x in f['reagents']}:
            reagent_graph[f['item_entry']].add(identity)
            reverse[('recipe_reagent',kind,f['source_entry'],identity,'recipe_reagent')]+=1
            provenance[('normalized.recipe_reagent',f['source_entry'],identity)]+=1
        if f['unresolved']:
            expected_incompatible.append(dict(source=key,field='unresolved',actual=f['unresolved'],consumer_error='condition_unknown'))
            unresolved.append(dict(source=key,reason='native_unresolved',details=f['unresolved']))
    for values,field,error in ((reverse,'reverse_identity','ambiguous_reverse_identity'),(provenance,'provenance','ambiguous_provenance')):
        for identity,count in values.items():
            if count>1:
                expected_incompatible.append(dict(field=field,identity=list(identity),actual=count,consumer_error=error))
    # Independent reachability oracle verifies cycle existence and each reported witness.
    def cyclic(root):
        pending=list(reagent_graph.get(root,()));visited=set()
        while pending:
            node=pending.pop()
            if node==root:return True
            if node not in visited:
                visited.add(node);pending.extend(reagent_graph.get(node,()))
        return False
    cycles=[x for x in evidence['consumer_incompatibilities'] if x['consumer_error']=='recipe_dependency_cycle']
    # Independently establish structural cycle presence by removing leaves.
    remaining={i:len(children) for i,children in reagent_graph.items()}
    parents=defaultdict(set)
    for item,children in reagent_graph.items():
        for child in children:
            remaining.setdefault(child,0);parents[child].add(item)
    pending=[i for i,count in remaining.items() if count==0];processed=0
    while pending:
        child=pending.pop();processed+=1
        for parent in parents[child]:
            remaining[parent]-=1
            if remaining[parent]==0:pending.append(parent)
    has_cycle=processed!=len(remaining)
    # Independent ordered native-call oracle. Native failure stops calls at depth 129,
    # before memo lookup; the entire input graph was already reconstructed above.
    native_states={}
    def visit(item,depth):
        if depth>128:return ('depth',depth)
        if native_states.get(item)==1:return ('cycle',item)
        if native_states.get(item)==2:return None
        native_states[item]=1
        for child in sorted(reagent_graph.get(item,())):
            failure=visit(child,depth+1)
            if failure:return failure
        native_states[item]=2
        return None
    failure=None
    for fact in sorted(expected_sources.values(),key=lambda f:(f['item_entry'],'source:'+sha(f['key']))):
        failure=visit(fact['item_entry'],0)
        if failure:break
    if failure and failure[0]=='depth':
        expected_incompatible.append(dict(field='craft_dependencies',actual=failure[1],consumer_error='recipe_dependency_depth'))
    if bool(cycles)!=has_cycle or len(cycles)>1 or any(not x['actual'] or any(not cyclic(i) for i in x['actual']) for x in cycles):
        raise CaptureError('independent reagent cycle evidence mismatch')
    equal(sorted([x for x in evidence['consumer_incompatibilities'] if x['consumer_error']!='recipe_dependency_cycle'],key=canonical),sorted(expected_incompatible,key=canonical),'consumer incompatibilities')
    equal(evidence['unresolved'],unresolved,'unresolved native source census')
    equal(evidence['consumer_compatible'],None,'consumer compatibility unverified')
    equal(evidence['selected_consumer_checks_passed'],not evidence['consumer_incompatibilities'],'consumer semantic check evidence')
    equal(evidence['consumer_validation'],'not_run','consumer validation scope')
    equal(manifest['warning_counts'],dict(unresolved=len(unresolved),consumer_incompatibilities=len(evidence['consumer_incompatibilities']),missing_stages=sum(not s['input_complete'] for s in policy['stages'])),'warning census')
    return dict(verified=True,objective_checks=objective_checks,oracle='sealed original raw/native export',sources=len(seen),raw_rows=sum(map(len,tables.values())),effective_items=len(projected),root_acceptance=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True);p.add_argument('--sha256',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    try:
        root=Path(a.output)
        docs={k:decode((root/(k+'.json')).read_bytes()) for k in ('graph','manifest','projection')}
        result=verify(a.manifest,a.sha256,docs,decode((root/'evidence.json').read_bytes()))
        sys.stdout.buffer.write(canonical(result)+b'\n');return 0
    except Exception as error:
        sys.stderr.write('REFUSED: '+str(error)+'\n');return 2

if __name__=='__main__':
    raise SystemExit(main())
