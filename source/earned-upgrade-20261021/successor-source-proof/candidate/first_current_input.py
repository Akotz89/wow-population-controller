"""OFF TICK automatic first-current collection from native seed receipts; never manual semantic JSON."""
import copy
import hashlib
from pathlib import Path
from capture_native_acquisition import CaptureError,canonical,read_descriptor,digest,QUEST_SOURCE_FIELDS,PYTHON_PREDICATE_HOOKS
from normalize_current_quest import normalize,read_json


HANDLER_SOURCE_MAP_SHA='4389751459b9bf752163b3568bd855198bca4542f16bb823a4c432b9f66f2203'

def handler_sources(original,input_path,expected_input_sha):
    """OFF TICK local WSL source proof, inseparable from captured source/attestation provenance."""
    observed={name for row in original['data']['context']['player_hook_dispatch'] for name in row['script_names']}
    if original['data']['native_slot']['native_quest'].get('source_rule_version')!='core1-ordinary-quest-source-rules-v1':return None,None,None
    try:
        input_path=Path(input_path);input_raw=input_path.read_bytes()
        if hashlib.sha256(input_raw).hexdigest()!=expected_input_sha:raise CaptureError('handler source input manifest custody')
        inputs=read_json(input_raw);attestation=read_json(read_descriptor(inputs['attestation'],input_path.parent))
        revision=digest(inputs['source_files'])
        if original['source_revision']!=revision or attestation['exporter_revision']!=revision or attestation['source_files_digest']!=revision or attestation['observation_generation']!=original['data']['context']['observation_generation'] or attestation['phase']!=original['data']['context']['phase'] or type(attestation['loaded_generation_verified']) is not bool:
            raise CaptureError('handler source original/native attestation source binding')
        path=Path(__file__).resolve().parents[1]/'evidence/native/CURRENT-HANDLER-SOURCE-MAP.json'
        descriptor=dict(path=str(path),bytes=path.stat().st_size,sha256=HANDLER_SOURCE_MAP_SHA,generation=original['snapshot_id'])
        raw=read_descriptor(descriptor,path.parent);proof=read_json(raw)
        if proof.get('schema')!='core1-current-player-quest-handler-source-map-v1' or proof.get('unresolved_direct_classes') or not proof.get('support_sources'):
            raise CaptureError('handler source subclass/support closure incomplete')
        overriding=proof['known_overriding_handlers'];defaults=proof['source_verified_default_only_handlers']
        if {row['script_name'] for row in overriding}!={'PythonPlayerScript','progression_system_shared_phase_gate'} or any(row['overrides'] for row in defaults):
            raise CaptureError('handler source override semantics changed')
        allowed={row['script_name'] for row in overriding+defaults}
        if not observed<=allowed:raise CaptureError('unknown enabled native quest handler: '+str(sorted(observed-allowed)))
        required=[dict(path=descriptor['path'],sha256=descriptor['sha256'])]+[proof['selected_vfs']]+overriding+defaults+proof['support_sources']
        required += [dict(path=row['constructor_path'],sha256=row['constructor_sha256']) for row in overriding+defaults if 'constructor_path' in row]
        captured={(row['sha256'],row['bytes']) for row in inputs['source_files']}
        sources={}
        for row in required:
            path=Path(row['path']);pin=dict(path=str(path),bytes=path.stat().st_size,sha256=row['sha256'],generation=original['snapshot_id'])
            read_descriptor(pin,path.parent)
            if (pin['sha256'],pin['bytes']) not in captured:raise CaptureError('handler proof source missing from captured source_files: '+str(path))
            sources[str(path)]=pin
        # Verify original capture descriptors, not just the presently readable local files.
        for descriptor in inputs['source_files']:read_descriptor(descriptor,input_path.parent)
        result=dict(allowed=sorted(allowed),observed=sorted(observed),descriptor=dict(path=str(Path(__file__).resolve().parents[1]/'evidence/native/CURRENT-HANDLER-SOURCE-MAP.json'),bytes=len(raw),sha256=HANDLER_SOURCE_MAP_SHA,generation=original['snapshot_id']),
            source_descriptors=list(sources.values()),source_files_digest=revision,exporter_revision=attestation['exporter_revision'],loaded_generation_verified=attestation['loaded_generation_verified'],scope='source-only exact selected composition; live binary/world closure verification remains Root acceptance')
        return result,raw,None
    except (CaptureError,OSError,KeyError,TypeError,ValueError) as error:
        return None,None,'Native quest handler source composition unresolved: '+str(error)


def source_semantics(slot,item,context,handler_proof=None,objective_proof=None):
    """OFF TICK: narrow representable ordinary quest rules; every unsupported branch remains explicit."""
    quest=slot['native_quest'];facts={};proof={};missing={}
    if not QUEST_SOURCE_FIELDS<=set(quest):return facts,proof,{field:'Original native loaded source-rule/giver/objective closure missing' for field in ('phase_mask','spawn_mask','event_ids','pool_ids','condition','group_size','capabilities','alternative','variant','reagents','unresolved')}
    proof['source_rule_version']=quest['source_rule_version']
    spawns=quest['giver_spawns']
    same_gates={(g['phase_mask'],g['spawn_mask'],tuple(g['event_ids']),tuple(g['pool_ids'])) for g in spawns}
    giver_complete=quest['giver_source_complete'] and not quest['giver_source_missing'] and bool(spawns) and len(same_gates)==1 and not quest['item_givers']
    if giver_complete:
        mask,spawn,events,pools=next(iter(same_gates))
        if all(event>0 for event in events):
            facts.update(phase_mask=mask,spawn_mask=spawn,event_ids=sorted(set(events)|set(quest['quest_event_ids'])|({quest['event_id']} if quest['event_id'] else set())),pool_ids=sorted(set(pools)|set(quest['quest_pool_ids'])))
            proof['availability']='complete loaded giver branches share exact masks/event/pool gates; native quest event/pool relations retained'
        else:missing['availability']='Negative event branch is not representable as unconditional positive event requirements'
    else:missing['availability']='Native giver census incomplete/empty or multiple distinct availability branches: '+str(quest['giver_source_missing'])
    if {'random_property','random_suffix'}<=set(item):
        facts['variant']={key:item[key] for key in ('random_property','random_suffix') if item[key]}
        proof['variant']='exact ItemTemplate random property/suffix fields; qualified native reward branch is separately custodied'
    else:missing['variant']='Native item random property/suffix fields unavailable'
    if not quest['exclusive_group'] and not quest['exclusive_quests']:
        facts['alternative']=None;proof['alternative']='native exclusive group zero and complete loaded exclusive member vector empty; selected quest rewarded-history is revalidated by caller'
    else:missing['alternative']='Native exclusive/prerequisite group requires additional complete mutually-exclusive history semantics'
    if all(not row['item'] and not row['count'] for field in ('required_items','required_source_items') for row in quest[field]) and not quest['source_item'] and not quest['source_item_count']:
        facts['reagents']=[];proof['reagents']='noncraft quest with every actual required/source item array slot and source item/count zero'
    else:missing['reagents']='Quest item objectives/source items require noncraft acquisition/dependency representation; never cast as craft reagents'
    callbacks=quest.get('python_predicate_callbacks',[])
    callbacks_known={r['hook_name'] for r in callbacks}==PYTHON_PREDICATE_HOOKS and all(r['registry_known'] and not r['reloading'] and not r['has_global'] for r in callbacks)
    handlers=quest['giver_scripts']
    observed_handlers={name for row in context.get('player_hook_dispatch',[]) for name in row['script_names']}
    native_dispatch_known=context.get('player_hook_registry_initialized') is True and (not observed_handlers or handler_proof is not None and observed_handlers<=set(handler_proof['allowed']))
    giver_scripts_known=set(quest['giver_entries'])<={g['entry'] for g in handlers} and all(g['template_known'] and g['registry_resolved'] and not g['script_id'] and not g['ai_name'] for g in handlers) and not any(g['script_id'] or g['multispawn'] or g['spawn_group_flags'] & ~1 for g in spawns)
    # Source-node primitive associations are exact accepted native mappings, not aggregate actor success.
    phase_policy=quest['phase_template_policy']
    phase_policy_known=phase_policy['known'] and phase_policy['configured'] and phase_policy['ready']
    leaves=[]
    if quest['quest_disabled'] or not phase_policy['template_allowed']:leaves.append(dict(op='false'))
    def leaf(kind,*values):return dict(op='leaf',condition_type=kind,values=list(values))
    for mask,kind in ((quest['required_classes'],15),(quest['allowable_races'],16)):
        if mask:leaves.append(leaf(kind,mask,0,0))
    if quest['required_skill']:leaves.append(leaf(7,quest['required_skill'],quest['required_skill_rank'],0))
    leaves.append(leaf(27,quest['minimum_level'],3,0))
    if quest['maximum_level']:leaves.append(leaf(27,quest['maximum_level'],4,0))
    groups={};conditions_supported=True
    for row in quest['conditions']:
        if row['source_type']!=19 or row['source_entry']!=quest['entry'] or row['source_group'] or row['source_id'] or row['target'] or row['reference_id'] or row['script_id'] or row['condition_type'] not in {15,16,7,27,8}:
            conditions_supported=False;continue
        expression=leaf(row['condition_type'],row['value1'],row['value2'],row['value3'])
        if row['negative']:expression=dict(op='not',argument=expression)
        groups.setdefault(row['else_group'],[]).append(expression)
    if groups:
        branches=[rows[0] if len(rows)==1 else dict(op='all',arguments=rows) for _,rows in sorted(groups.items())]
        leaves.append(branches[0] if len(branches)==1 else dict(op='any',arguments=branches))
    unrepresented=bool(quest['previous_quest_id'] or quest['next_quest_in_chain'] or quest['previous_quests'] or quest['previous_chain_quests'] or quest['exclusive_group'] or quest['exclusive_quests'] or quest['breadcrumb_quest'] or quest['minimum_rep_faction'] or quest['maximum_rep_faction'] or quest['source_spell'] or quest['time_allowed'] or quest['reward_mail_template'] or any(quest[f] for f in ('repeatable','daily','weekly','monthly','seasonal','dungeon_finder')))
    completion_fields={'rep_objective_faction','rep_objective_value','rep_objective_faction2','rep_objective_value2','breadcrumb_dependents','timed','exploration_or_event','cast_objective','required_player_kills'}
    completion_known=completion_fields<=set(quest)
    ordinary_static=objective_proof is not None and objective_proof.get('static_status')=='PROVEN'
    unrepresented=unrepresented or not completion_known or any(quest[field] for field in completion_fields if field in quest and not (field=='cast_objective' and ordinary_static))
    level=quest['native_quest_level'];hide=quest['native_low_level_hide_diff']
    representable=True
    if level>0:
        upper=level+hide
        representable=upper<=2**32-1
        if representable:
            leaves.append(dict(op='false') if upper<1 else leaf(27,upper,4,0))
            if level>quest['minimum_level']+3:leaves.append(leaf(27,level-3,3,0))
    else:
        # Player::GetQuestLevel uses actor level for every native QuestLevel<=0.
        representable=hide<=(2**32-1)-255  # no uint32 wrap for the full native uint8 actor-level domain.
    phase=context['phase']
    phase_bound_implied=phase['phase_id']=='1_19' and phase['level_ceiling']==19 and (0<quest['maximum_level']<=19 or level>0 and level+hide<=19)
    capable=phase_bound_implied and quest['type']==0 and quest['native_suggested_players']<2 and representable
    exact_skill_predicates=not item['required_skill'] and not (quest['required_skill'] and not quest['required_skill_rank'])
    static_condition_known=exact_skill_predicates and phase_policy_known and callbacks_known and native_dispatch_known and giver_scripts_known and conditions_supported and not unrepresented and capable and 'reagents' in facts
    if ordinary_static and static_condition_known:
        proof['ordinary_condition_ast']=leaves[0] if len(leaves)==1 else dict(op='all',arguments=leaves)
    effect_known=not ordinary_static  # Static objective evidence cannot authorize the absent native effect binding.
    if effect_known and exact_skill_predicates and phase_policy_known and callbacks_known and native_dispatch_known and giver_scripts_known and conditions_supported and not unrepresented and capable and 'reagents' in facts:
        facts['condition']=leaves[0] if len(leaves)==1 else dict(op='all',arguments=leaves)
        facts.update(group_size=1,capabilities=[])
        proof['condition']='actual quest/template and QUEST_AVAILABLE positive primitive AST + negative/else group composition; current actor verdicts only via accepted source-predicate context, never universal true'
        proof['group_size/capabilities']='core1 ordinary quest source rules: IsQuestCapableDoing solo executor type0, SuggestedPlayers<2 applicability only, actual signed quest level/uint32 config worth/capability bounds represented by exact source-associated level predicates (dynamic<=0 case proven from Player::GetQuestLevel); existing admitted solo native action revalidates and requires no additional named planner capabilities'
    else:
        missing['condition']=objective_proof['effect_custody']['missing_native_binding'] if ordinary_static else 'Native progression hook actor-level ceiling19 is not implied by represented source bounds' if not phase_bound_implied else 'Unrepresented native quest prerequisite/reputation/completion/breadcrumb/timer/repeat/mail/handler/giver/action-capability or item-effective/quest-rank0 skill semantics; unsupported source remains nonexecutable'
    if objective_proof is not None and not ordinary_static and 'condition' in missing:
        missing['condition']='Ordinary positive-target static proof UNKNOWN: '+'; '.join(objective_proof.get('missing',[]))+'; '+missing['condition']
    if all(field in facts for field in ('phase_mask','spawn_mask','event_ids','pool_ids','variant','alternative','reagents','condition','group_size','capabilities')):
        facts['unresolved']=[];proof['unresolved']='all supported source predicates represented; actor leaves remain dynamically revalidated or UNKNOWN'
    for label,fields in [('availability',('phase_mask','spawn_mask','event_ids','pool_ids')),('condition',('group_size','capabilities','unresolved'))]:
        if label in missing:
            for field in fields:
                if field not in facts:missing[field]=missing[label]
    return facts,proof,{field:reason for field,reason in missing.items() if field not in facts and field!='availability'}


def collect(original_descriptor, original_root, policy_descriptor, policy_root, input_path, expected_input_sha, output):
    """Retain original bytes; derive only exported template facts. Unsupported predicates stay nonexecutable."""
    from capture_manifest import collect_current_evidence
    raw=read_descriptor(original_descriptor,Path(original_root));original=read_json(raw);data=original['data']
    metrics=normalize(original_descriptor,original_root,policy_descriptor,policy_root)
    if data.get('actor_observation') is None:raise CaptureError('no original admitted callback observation available')
    slot=data['native_slot'];quest=slot['native_quest'];item=next(row for row in data['context']['items'] if row['entry']==slot['item_entry'])
    facts={'family':'quest_reward'}  # Actual selected QuestTemplate reward branch, not dungeon-family completeness.
    source_proofs={'family':'original loaded QuestTemplate reward slot provenance'}
    handler_proof,handler_raw,handler_error=handler_sources(original,input_path,expected_input_sha)
    from ordinary_kill_proof import derive
    objective_proof=derive(quest,data['context'],input_path,expected_input_sha) if quest.get('cast_objective') else None
    if objective_proof is not None:source_proofs['ordinary_positive_target']=objective_proof
    supported,supported_proofs,source_missing=source_semantics(slot,item,data['context'],handler_proof,objective_proof)
    if handler_error:
        for field in ('condition','group_size','capabilities','unresolved'):source_missing[field]=handler_error
    if handler_proof is not None:source_proofs['native_handler_source_map']=handler_proof
    facts.update(supported);source_proofs.update(supported_proofs)
    if handler_error:
        for field in ('condition','group_size','capabilities','unresolved'):facts.pop(field,None)
    witnesses=metrics['data']['source_witnesses']
    if witnesses:
        facts['probability']=[1,1]
        source_proofs['probability']=dict(policy_descriptor=copy.deepcopy(policy_descriptor),meaning='conditional exact native reward branch after successful validated turn-in; not quest completion probability',records=[w['record_id'] for w in witnesses])
    actor=data['actor_observation']['data']['actor'] if data.get('actor_observation') is not None else None
    phase=data['context']['phase']
    offered=[] if actor is None else [r for q in actor['gear']['quests'] if q['quest_id']==slot['source_entry'] for r in q['rewards'] if (r['kind'],r['index'],r['item']['entry'])==(slot['native_reward']['kind'],slot['native_reward']['index'],slot['item_entry'])]
    source_item_intersection=actor is not None and actor['phase']==phase and phase['enabled'] is True and phase['policy_valid'] is True and phase['phase_id']=='1_19' and phase['level_ceiling']==19 and quest['minimum_level']<=actor['gear']['level']<=min(19,quest['maximum_level'] or 19) and item['required_level']<=actor['gear']['level'] and len(offered)==1 and offered[0].get('classification_allowed') is True and offered[0].get('requirement_result')==0
    if source_item_intersection and any(w['source_prerequisite_eligibility'] for w in witnesses):
        facts.update(stages=['S01'],frontier=[1,1])
        source_proofs['stages/frontier']=dict(policy_descriptor=copy.deepcopy(policy_descriptor),phase=copy.deepcopy(phase),records=[w['record_id'] for w in witnesses if w['source_prerequisite_eligibility']],scope='accepted current-only plannerphase1; witnessed source/item/prerequisite eligibility intersection; future unknown')

    # These exact exports establish charges and skill requirements, not universal eligibility or script coverage.
    facts['copper']=max(0,-quest['signed_money'])
    skills={(q,r) for q,r in ((quest['required_skill'],quest['required_skill_rank']),(item['required_skill'],item['required_skill_rank'])) if q}
    if len(skills)<=1:facts['profession']=list(next(iter(skills))) if skills else [0,0]
    # Opaque native slot custody is never passed as semantic variant or silently rewritten condition=None.
    derived=dict(snapshot_id=original['snapshot_id'],kind='native_execution',source_revision=original['source_revision'],
        data=dict(slots={slot['key']:facts},source_proofs=source_proofs,source_missing={slot['key']:source_missing},original_receipt=copy.deepcopy(original_descriptor),
            scope='exported template facts only; script/condition/phase/spawn/event/pool/group/capability semantics remain unresolved'))
    files={};receipts=[]
    def add(name,content,is_receipt=True):
        name='native-derived-'+name+'.json';files[name]=content
        descriptor=dict(path=name,bytes=len(content),sha256=hashlib.sha256(content).hexdigest(),generation=original['snapshot_id'])
        if is_receipt:receipts.append(descriptor)
        return descriptor
    add('original',raw)
    if handler_raw is not None:source_proofs['native_handler_source_map']['retained_original']=add('handler-map',handler_raw,False)
    add('predicates',canonical(derived)+b'\n')
    add('metrics',canonical(metrics)+b'\n')
    callback=add('callback',canonical(data['actor_observation'])+b'\n',False)
    return collect_current_evidence(callback,data['identity_key'],slot['source_entry'],input_path,expected_input_sha,output,
        _derived_files=files,_derived_receipts=receipts)
