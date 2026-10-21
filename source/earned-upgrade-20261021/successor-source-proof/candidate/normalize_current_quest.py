"""OFF-TICK current-quest-observation-v1 estimates from byte-custodied original native journals."""
import copy
import hashlib
import json
from pathlib import Path
from capture_native_acquisition import CaptureError, canonical, read_descriptor, _uint, native_observations, shape, PYTHON_PREDICATE_HOOKS

POLICY='current-quest-observation-v1'
PLAN_SHA='643f2941bd1373d0485d87722a01f434aa1496ad7f9b7aadd1ea83e14ba310a7'
CLARIFICATION_SHA='22a2ce98f4c683de085d24813f712ae7fec3d32d9b7a81e7ce82760df5e7aaa7'
BINDING=('identity_key','account_id','character_guid','session_incarnation','strategic_generation','goal_revision','phase_digest','source_key','quest_id','reward_kind','reward_index','item_entry','reward_count')

def require(test,reason):
    if not test:raise CaptureError(reason)

def read_json(data):
    def pairs(rows):
        value={}
        for key,item in rows:
            require(key not in value,'duplicate original native JSON key');value[key]=item
        return value
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(CaptureError('nonfinite native observation')))

MAIL_KINDS={'mail_delivery_pending','mail_delivery_committed','mail_delivery_failed','mail_delivery_unknown'}
LATE_CONTINUITY={'logout','invalidated','reset'}
DIGEST_V2='core1-native-acquisition-record-digest-v2'

def native_receipts(attempt,slot,owned_operations):
    """Validate typed original receipt/event associations; retain unknown outcomes without inventing delivery."""
    events={event['event_sequence']:event for event in attempt['events']};operations={};selected=[];issues=[]
    rows=attempt.get('reward_receipts',[])
    require(isinstance(rows,list),'native reward receipt vector')
    fields={'event_sequence','monotonic_ticks','quest_id','reward_kind','reward_index','item_entry','count','count_sum','complete','delivery_reason','record_id','operation_id','delivery_pending','delivery_known','transaction_committed','queued_complete','destinations','mail_envelopes'}
    for receipt in rows:
        shape(receipt,fields,'native reward receipt fields')
        for field in ('event_sequence','monotonic_ticks','count_sum','record_id','operation_id'):_uint(receipt[field],2**64-1)
        for field in ('quest_id','reward_index','item_entry','count'):_uint(receipt[field])
        for field in ('complete','delivery_pending','delivery_known','transaction_committed','queued_complete'):require(type(receipt[field]) is bool,'native reward receipt boolean')
        require(receipt['record_id']==attempt['record_id'] and isinstance(receipt['delivery_reason'],str),'native reward receipt original record binding')
        require(receipt['reward_kind'] in {'guaranteed','choice'},'native reward receipt kind')
        event=events.get(receipt['event_sequence']);op=receipt['operation_id']
        require(event is not None and event['monotonic_ticks']==receipt['monotonic_ticks'] and event.get('operation_id')==op and event['item_entry']==receipt['item_entry'] and event['count']==receipt['count'],'native reward receipt event/operation binding')
        require(isinstance(receipt['destinations'],list) and isinstance(receipt['mail_envelopes'],list),'native reward destination vectors')
        native_kind='fixed' if receipt['reward_kind']=='guaranteed' else 'choice'
        branch=(receipt['quest_id']==slot['source_entry'] and any((r['kind'],r['index'],r['item'],r['count'])==(native_kind,receipt['reward_index'],receipt['item_entry'],receipt['count']) for r in slot['native_quest']['rewards']))
        require(branch or not attempt['valid_window'] and not receipt['complete'] and not receipt['delivery_known'],'native reward receipt catalogue slot/count binding')
        inventory_sum=0;inventory_exact=bool(receipt['destinations'])
        for destination in receipt['destinations']:
            shape(destination,{'bag_slot','planned_count','before_guid','before_entry','before_count','item_guid','item_entry','after_count','delta','matched'},'native inventory destination fields')
            for field in set(destination)-{'matched'}:_uint(destination[field],2**64-1 if field in {'before_guid','item_guid'} else 65535 if field=='bag_slot' else 2**32-1)
            require(type(destination['matched']) is bool,'native inventory destination matched flag')
            exact=destination['matched'] and destination['item_guid']>0 and destination['item_entry']==receipt['item_entry'] and destination['after_count']>=destination['before_count'] and destination['delta']==destination['after_count']-destination['before_count']==destination['planned_count']
            if destination['before_count']:exact=exact and destination['before_guid']==destination['item_guid'] and destination['before_entry']==destination['item_entry']
            inventory_exact=inventory_exact and exact;inventory_sum+=destination['delta']
        mail_sum=0;mail_exact=bool(receipt['mail_envelopes']);mail_ids=set();item_guids=set()
        for envelope in receipt['mail_envelopes']:
            shape(envelope,{'mail_id','receiver_guid','deliver_time','queued','attachments'},'native mail envelope fields')
            for field in ('mail_id','receiver_guid','deliver_time'):_uint(envelope[field],2**64-1 if field=='deliver_time' else 2**32-1)
            require(type(envelope['queued']) is bool and isinstance(envelope['attachments'],list),'native mail envelope values')
            mail_exact=mail_exact and envelope['queued'] and envelope['receiver_guid']==attempt['character_guid'] and envelope['mail_id']>0 and envelope['mail_id'] not in mail_ids and bool(envelope['attachments'])
            mail_ids.add(envelope['mail_id'])
            for attachment in envelope['attachments']:
                shape(attachment,{'item_guid','item_entry','count','saved_entry','saved_count','saved_value_matched'},'native mail attachment fields')
                for field in set(attachment)-{'saved_value_matched'}:_uint(attachment[field],2**64-1 if field=='item_guid' else 2**32-1)
                require(type(attachment['saved_value_matched']) is bool,'native mail saved-value flag')
                mail_exact=mail_exact and attachment['saved_value_matched'] and attachment['item_guid']>0 and attachment['item_guid'] not in item_guids and attachment['item_entry']==attachment['saved_entry']==receipt['item_entry'] and attachment['count']==attachment['saved_count'] and attachment['count']>0
                item_guids.add(attachment['item_guid']);mail_sum+=attachment['count']
        if not op:
            require(event['event_kind']=='reward_destinations' and not receipt['mail_envelopes'] and not receipt['delivery_pending'],'native inventory receipt association')
            if receipt['complete']:require(inventory_exact and inventory_sum==receipt['count_sum']==receipt['count'],'native complete inventory destination proof')
        else:
            require(event['event_kind'] in MAIL_KINDS and not receipt['destinations'],'native mail receipt event kind')
            owner=(attempt['record_id'],receipt['quest_id'],receipt['reward_kind'],receipt['reward_index'],receipt['item_entry'],receipt['count'])
            prior_owner=owned_operations.get(op)
            require(prior_owner is None or prior_owner==owner or prior_owner[0]==owner[0] and not attempt['valid_window'] and not receipt['delivery_known'] and not receipt['complete'],'native mail operation reused across owner/source')
            if prior_owner is None:owned_operations[op]=owner
            previous=operations.get(op)
            if previous is not None:
                require(previous['delivery_pending'] and not receipt['delivery_pending'],'native mail operation outcome duplicated/reopened')
                require(all(previous[f]==receipt[f] for f in ('record_id','quest_id','reward_kind','reward_index','item_entry','count')) or not attempt['valid_window'] and not receipt['delivery_known'],'native mail operation original slot drift')
            elif not receipt['delivery_pending']:
                require(event['event_kind']=='mail_delivery_unknown' and event['measurement_scope']=='action_window' and not receipt['delivery_known'],'native mail final outcome missing original pending operation')
            if receipt['delivery_pending']:
                require(previous is None and event['event_kind']=='mail_delivery_pending' and event['measurement_scope']=='action_window' and not receipt['complete'] and not receipt['delivery_known'] and not receipt['transaction_committed'],'native pending mail outcome flags')
            else:
                expected='mail_delivery_unknown' if not receipt['delivery_known'] else 'mail_delivery_committed' if receipt['transaction_committed'] else 'mail_delivery_failed'
                require(event['event_kind']==expected,'native mail outcome/commit flags')
            if receipt['queued_complete']:require(mail_exact and mail_sum==receipt['count_sum']==receipt['count'],'native mail queued attachment/saved-value proof')
            require(not receipt['complete'] or receipt['queued_complete'] and receipt['delivery_known'] and receipt['transaction_committed'] and not receipt['delivery_pending'],'native complete mail requires committed exact queued receipt')
            operations[op]=receipt
        if (receipt['quest_id'],receipt['reward_kind'],receipt['reward_index'],receipt['item_entry'],receipt['count'])==(attempt['quest_id'],attempt['reward_kind'],attempt['reward_index'],attempt['item_entry'],attempt['reward_count']):selected.append(receipt)
    for event in attempt['events']:
        if event['event_kind'] in MAIL_KINDS:
            require(any(r['event_sequence']==event['event_sequence'] for r in rows),'native mail event lacks exact original receipt')
    pending=sum(row['delivery_pending'] for row in operations.values())
    if 'pending_deliveries' in attempt:
        _uint(attempt['pending_deliveries'],2**64-1);require(attempt['pending_deliveries']==pending,'native pending delivery operation census')
    if attempt.get('terminal') is True:require(pending==0,'native final record has pending delivery')
    if attempt.get('complete_destination_attribution') is True:
        require(selected and selected[-1]['complete'],'native claimed complete attribution lacks exact selected receipt')
    if selected and not selected[-1]['complete']:issues.append('selected native delivery attribution UNKNOWN: '+selected[-1]['delivery_reason'])
    return operations,issues

def normalize(original_descriptor, original_root, policy_descriptor, policy_root):
    """No native calls. Preserve every raw outcome/window; missing coverage is UNKNOWN, never zero."""
    original_raw=read_descriptor(original_descriptor,Path(original_root));original=read_json(original_raw)
    policy_raw=read_descriptor(policy_descriptor,Path(policy_root));policy=read_json(policy_raw)
    require(policy.get('policy_id')==POLICY and policy.get('authority',{}).get('sha256')==PLAN_SHA,'accepted current quest policy identity')
    authority=policy['authority'];plan=Path(authority['path']).read_bytes()
    require(hashlib.sha256(plan).hexdigest()==PLAN_SHA,'accepted original plan custody')
    clarification=policy.get('clarification',{})
    require(clarification.get('sha256')==CLARIFICATION_SHA and hashlib.sha256(Path(clarification['path']).read_bytes()).hexdigest()==CLARIFICATION_SHA,'accepted measurement clarification custody')
    require(original.get('kind')=='native_execution' and original.get('snapshot_id')==original_descriptor.get('generation'),'native original receipt provenance')
    data=original['data'];slot=data['native_slot'];context=data['context'];identity=data['identity_key'];account,guid=data['binding']
    require(data.get('schema') in {'core1-original-native-quest-observations-v1','core1-original-native-quest-observations-v2'},'native original journal schema')
    reward=slot['native_reward'];native_kind='guaranteed' if reward['kind']=='fixed' else 'choice'
    expected_source=f"quest:{slot['source_entry']}:{native_kind}:{reward['index']}:{slot['item_entry']}"
    journal={};missing=[]
    for snapshot in native_observations(data):
        require(snapshot.get('schema')=='core1-native-quest-acquisition-v1','native snapshot schema')
        require(snapshot.get('identity_key')==identity and snapshot.get('account_id')==account and snapshot.get('character_guid')==guid,'native snapshot actor binding')
        if snapshot.get('valid') is not True:
            missing.append('native snapshot invalid: '+str(snapshot.get('reason')))
        require(isinstance(snapshot.get('attempts'),list),'native attempt list')
        if not snapshot['attempts']:missing.append('no actual native observation')
        for attempt in snapshot['attempts']:
            require(all(field in attempt for field in BINDING),'native attempt complete owner binding')
            require((attempt['identity_key'],attempt['account_id'],attempt['character_guid'])==(identity,account,guid),'native attempt actor drift')
            require((attempt['source_key'],attempt['quest_id'],attempt['reward_kind'],attempt['reward_index'],attempt['item_entry'],attempt['reward_count'])==(expected_source,slot['source_entry'],native_kind,reward['index'],slot['item_entry'],slot['minimum_count']),'native exact source/reward slot/count binding')
            require(attempt.get('monotonic_frequency')==1000000000,'native declared monotonic frequency')
            for field in ('record_id','session_incarnation','goal_revision','strategic_generation'):_uint(attempt[field],2**64-1)
            key=attempt['record_id'];old=journal.get(key)
            if old is not None:
                require(all(old[f]==attempt[f] for f in BINDING),'native attempt owner cannot change across snapshots')
                require(attempt['events'][:len(old['events'])]==old['events'],'native event history changed/truncated')
                if old['terminal']:require({k:v for k,v in attempt.items() if k!='current_context_matches'}=={k:v for k,v in old.items() if k!='current_context_matches'},'native terminal record mutated')
            journal[key]=copy.deepcopy(attempt)
    metrics=[];valid=[];owned_sequences=set();owned_operations={};source_witnesses=[]
    for record_id,attempt in sorted(journal.items()):
        events=attempt['events'];require(isinstance(events,list),'native events array')
        previous_sequence=0;previous_time=None
        for event in events:
            _uint(event['event_sequence'],2**64-1);_uint(event['monotonic_ticks'],2**64-1)
            require(event['event_sequence']>previous_sequence and (previous_time is None or event['monotonic_ticks']>=previous_time),'native event sequence/time monotonic')
            previous_sequence=event['event_sequence'];previous_time=event['monotonic_ticks']
            require(event['event_sequence'] not in owned_sequences,'native event sequence reused across attempts');owned_sequences.add(event['event_sequence'])
            require(type(event['outcome']) is bool,'native event outcome type')
            if 'operation_id' in event:_uint(event['operation_id'],2**64-1)
            if 'measurement_scope' in event:require(event['measurement_scope'] in {'action_window','delivery_after_action'},'native event measurement scope')
        accepted=[e for e in events if e['event_kind']=='accepted'];terminal=[e for e in events if e['event_kind'] in {'turnin_end','attempt_terminal'}]
        reasons=[]
        scoped=all('operation_id' in event and 'measurement_scope' in event for event in events)
        if not scoped:reasons.append('native event scope/operation coverage UNKNOWN')
        if 'record_digest_version' in attempt:
            require(attempt['record_digest_version']==DIGEST_V2 and 'record_sha256' in attempt,'native record digest version/hash')
            require(type(attempt.get('action_terminal')) is bool and (not attempt['terminal'] or attempt['action_terminal'] is True),'native final/action terminal flags')
            if 'post_action_context_lost' in attempt:require(type(attempt['post_action_context_lost']) is bool,'native post-action context flag')
        if 'record_sha256' in attempt:
            value=attempt['record_sha256']
            require(isinstance(value,str) and ((len(value)==64 and all(c in '0123456789abcdefABCDEF' for c in value)) if attempt['terminal'] else value==''),'native original record hash representation')
        delivery_issues=[]
        if scoped:
            operations,delivery_issues=native_receipts(attempt,slot,owned_operations)
        if data.get('capture_records') is not None:
            binding=data['capture_records'].get(str(record_id))
            if binding is None:reasons.append('outside original finite capture finalization ownership')
            else:require(all(attempt[field]==binding[field] for field in BINDING),'original finite capture owner binding')
        if attempt.get('observer_failure') is not False:reasons.append('native observer failure or unknown observer coverage')
        if attempt['strategic_generation']!=context['observation_generation'] or attempt['phase_digest']!=context['phase']['policy_digest']:reasons.append('original source generation/phase drift')
        if attempt.get('valid_window') is not True or attempt.get('terminal') is not True or attempt.get('valid_attempt') is not True:reasons.append('incomplete/invalid native terminal attempt')
        if any(e['event_kind'] in {'logout','invalidated','reset'} and e.get('measurement_scope')=='action_window' for e in events):reasons.append('native interrupted window')
        if len(accepted)!=1 or len(terminal)!=1:reasons.append('exact accepted/terminal window missing')
        record=dict(record_id=record_id,binding={f:attempt[f] for f in BINDING},terminal_outcome=attempt['terminal_outcome'],
            raw_event_sequences=[e['event_sequence'] for e in events],valid=False,missing=reasons,metrics={},
            coverage={f:attempt.get(f) for f in ('movement_coverage','death_coverage','route_coverage','complete_destination_attribution','failure_terminal_coverage','observer_failure')},
            scopes={f:attempt.get(f) for f in ('movement_scope','route_scope','predicate_scope','reward_receipt_scope')})
        if not reasons:
            begin,end=accepted[0]['monotonic_ticks'],terminal[0]['monotonic_ticks']
            require(end>=begin,'native accepted-to-terminal time order')
            require(all(e.get('measurement_scope')=='action_window' and e.get('operation_id')==0 for e in accepted+terminal),'native accepted/terminal action scope')
            intervals=[];moving=None;action_events=[]
            for event in events:
                t=event['monotonic_ticks']
                require(t>=begin,'native event before accepted window')
                if event['measurement_scope']=='delivery_after_action':
                    require(event['event_sequence']>terminal[0]['event_sequence'] and t>=end,'native late event precedes action terminal')
                    require(event['event_kind'] in (MAIL_KINDS-{'mail_delivery_pending'}) | LATE_CONTINUITY,'undeclared native event after measured action window')
                    require(event['operation_id']>0 if event['event_kind'] in MAIL_KINDS else event['operation_id']==0,'native late event operation association')
                    record.setdefault('late_delivery_events' if event['event_kind'] in MAIL_KINDS else 'late_continuity_events',[]).append(copy.deepcopy(event))
                    continue
                require(t<=end and event['event_sequence']<=terminal[0]['event_sequence'],'undeclared native event after measured action window')
                if event['event_kind'] not in MAIL_KINDS:require(event['operation_id']==0,'native ordinary action event operation')
                action_events.append(event)
                if event['event_kind']=='move_begin':
                    require(moving is None,'overlapping/unpaired native move begin');moving=t
                elif event['event_kind']=='move_end':
                    require(moving is not None and t>=moving,'native movement end without begin');intervals.append([moving,t]);moving=None
            record['window']=[begin,end];record['movement_intervals']=intervals
            record['measurement_scope']='accepted_to_native_action_terminal; declared late delivery events retained outside action duration'
            elapsed=end-begin;travel=sum(b-a for a,b in intervals)
            require(travel<=elapsed,'native movement exceeds elapsed')
            coverage=record['coverage'];counts=dict(valid_attempts=1,deaths=sum(e['event_kind']=='death' for e in action_events),
                qualifying_routes=sum(e['event_kind']=='route_decision' for e in action_events),
                blocked_routes=sum(e['event_kind']=='route_decision' and e['outcome'] is False for e in action_events))
            record.update(valid=True,counts=counts,elapsed_ticks=elapsed,travel_ticks=travel,remaining_ticks=elapsed-travel,unit='nanoseconds')
            if coverage['movement_coverage'] is True and moving is None:
                record['metrics'].update(travel_seconds=(travel+999999999)//1000000000,seconds_per_attempt=(elapsed-travel+999999999)//1000000000)
            else:record['missing'].append('movement coverage/open interval UNKNOWN')
            if coverage['death_coverage'] is True and coverage.get('failure_terminal_coverage') is True:record['metrics']['risk_milli']=counts['deaths']*1000
            else:record['missing'].append('death/failure terminal coverage UNKNOWN')
            if coverage['route_coverage'] is True and counts['qualifying_routes']:
                n,d=counts['blocked_routes']*1000,counts['qualifying_routes'];record['metrics']['contention_milli']=(2*n+d)//(2*d)
            else:record['missing'].append('route coverage/zero denominator UNKNOWN')
            if attempt['terminal_outcome']=='reward_quest_completed' and coverage['complete_destination_attribution'] is True:
                positives={p['name'] for p in attempt['predicates'] if p.get('evaluated') is True and p.get('result') is True and p.get('scope') in {'SetQuestGoal.FindInactiveQuestGiver.actor_session_goal_phase','ordinary_native_action.actor_session_goal_phase'}}
                source_witnesses.append(dict(record_id=record_id,binding=copy.deepcopy(record['binding']),conditional_successful_reward=True,source_prerequisite_eligibility={'CanTakeQuest','CanAddQuest'}<=positives,scope='original actor/session/goal/phase only; no universal predicate truth'))
            valid.append(record)
        record['delivery_missing']=delivery_issues
        record['record_sha256']=attempt.get('record_sha256')
        record['record_digest_version']=attempt.get('record_digest_version')
        metrics.append(record)
    combined={};all_missing=list(missing)
    all_missing.extend(reason for record in metrics for reason in record['missing'])
    compatible={tuple(record['binding'][f] for f in ('identity_key','account_id','character_guid','session_incarnation','strategic_generation','phase_digest','source_key','reward_count')) for record in valid}
    if len(compatible)>1:
        all_missing.append('incompatible owner/session/phase/source groups cannot be aggregated')
    totals=dict(valid_attempts=len(valid),elapsed_ticks=sum(r['elapsed_ticks'] for r in valid),travel_ticks=sum(r['travel_ticks'] for r in valid),remaining_ticks=sum(r['remaining_ticks'] for r in valid),deaths=sum(r['counts']['deaths'] for r in valid),qualifying_routes=sum(r['counts']['qualifying_routes'] for r in valid),blocked_routes=sum(r['counts']['blocked_routes'] for r in valid),unit='nanoseconds')
    if valid and len(compatible)==1 and all(r['coverage']['movement_coverage'] is True and 'movement coverage/open interval UNKNOWN' not in r['missing'] for r in valid):
        divisor=len(valid)*1000000000
        combined.update(travel_seconds=(totals['travel_ticks']+divisor-1)//divisor,seconds_per_attempt=(totals['remaining_ticks']+divisor-1)//divisor)
    if valid:
        if len(compatible)==1 and all(r['coverage']['death_coverage'] is True and r['coverage'].get('failure_terminal_coverage') is True for r in valid):
            n=sum(r['counts']['deaths'] for r in valid)*1000;d=len(valid);combined['risk_milli']=(2*n+d)//(2*d)
        total=sum(r['counts']['qualifying_routes'] for r in valid)
        if len(compatible)==1 and all(r['coverage']['route_coverage'] is True for r in valid) and total:
            n=sum(r['counts']['blocked_routes'] for r in valid)*1000;combined['contention_milli']=(2*n+total)//(2*total)
    else:all_missing.append('no valid measured attempt; all metrics UNKNOWN')
    if data.get('state')=='refused':all_missing.append('collector lifetime invalidated: '+str(data.get('error')))
    # Complete item destination and universal predicate scope are independent from measured estimates.
    source_missing=['native conditional successful reward all-destination/choice attribution missing'] if not source_witnesses else []
    source_missing.extend(reason for record in metrics for reason in record['delivery_missing'])
    callbacks=slot['native_quest'].get('python_predicate_callbacks')
    if callbacks is None or {row['hook_name'] for row in callbacks}!=PYTHON_PREDICATE_HOOKS:source_missing.append('passive Python predicate registry coverage UNKNOWN')
    elif any(not row['registry_known'] or row['reloading'] for row in callbacks):source_missing.append('passive Python predicate registry not stable/known')
    elif any(row['has_global'] for row in callbacks):source_missing.append('actual global0 Python predicate callbacks require scoped handler semantics proof')
    # Exact-entry-only callbacks are retained facts; the current macro invokes entry0. Absence is never universal predicate truth.
    if context.get('player_hook_registry_initialized') is not True:source_missing.append('native player hook registry not initialized')
    handlers=[dict(hook=dispatch['hook_name'],scripts=dispatch['script_names']) for dispatch in context.get('player_hook_dispatch',[]) if dispatch['script_names']]
    if handlers:source_missing.append('actual enabled hook handler bodies require source semantics proof: '+str(handlers))
    if slot['native_quest'].get('conditions'):source_missing.append('actual ConditionMgr rows require supported universal condition representation')
    giver_handlers=[g for g in slot['native_quest'].get('giver_scripts',[]) if not g['template_known'] or not g['registry_resolved'] or g['script_id'] or g['ai_name']]
    if giver_handlers:source_missing.append('actual giver/taker script/AI handlers require scoped source semantics proof')
    return dict(snapshot_id=original['snapshot_id'],kind='measurement',source_revision=original['source_revision'],
        data=dict(policy_id=POLICY,policy_descriptor=copy.deepcopy(policy_descriptor),original_descriptor=copy.deepcopy(original_descriptor),
            native_slot_key=slot['key'],attempts=metrics,metrics=combined,missing=all_missing,source_missing=source_missing,
            totals=totals,source_witnesses=source_witnesses,slots={slot['key']:{out:combined[field] for out,field in {'travel':'travel_seconds','attempt':'seconds_per_attempt','risk':'risk_milli','contention':'contention_milli'}.items() if field in combined}},
            ready=False,interpretation='observed estimates; all raw outcome windows retained; not probability or universal source acceptance'))
