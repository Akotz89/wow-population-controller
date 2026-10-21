"""Existing native quest executor seed; no graph, accepted dataset or alternate controller."""
import copy
import math
from quest_policy import QuestPolicy, ProtocolError


class NativeSeedPolicy(QuestPolicy):
    policy_version = 'current-native-quest-seed-v1'

    def __init__(self, native, audit, *, prepared_catalogue, **kwargs):
        # Prepared once OFF TICK; original catalogue bytes/hash retained by bootstrap.
        self.catalogue = copy.deepcopy(prepared_catalogue)
        self.quest_ids = tuple(sorted(q['entry'] for q in self.catalogue['quests'] if q['rewards']))
        self.slots = {q['entry']: {(r['kind'],r['index']):(r['item'],r['count']) for r in q['rewards']}
                      for q in self.catalogue['quests']}
        self.cursor_by_actor = {}; self.selected_reward = None
        super().__init__(native, audit, **kwargs)

    def tick(self, bindings):
        observer = self.acquisition_capture
        before = getattr(observer, 'before_strategy_tick', None)
        if callable(before):
            try:before(bindings, self.source_sha256)
            except Exception as error:observer.fail(type(error).__name__ + ': ' + str(error))
        return super().tick(bindings)

    def _pending_goal(self, quest, snapshot):
        return (quest in self.slots and snapshot['current_goal_quest_id'] == quest
                and snapshot.get('current_goal_acquiring') is True)

    def _set_goal(self, key, quest):
        if self.selected_reward is None:
            raise ProtocolError('native seed handoff requires exact observed reward slot')
        return self.native.strategic_set_quest_goal(key, quest, *self.selected_reward)

    def _select_goal(self, key, binding, snapshot, eligible, now):
        self.selected_reward = None
        self._capture_event('selecting',key,binding)
        if not self.quest_ids:
            self._emit(key,'deferred',0,'loaded_catalogue_has_no_reward_slots');return None
        previous=self.goals.get(key,0)
        if previous in self.slots and snapshot['current_goal_quest_id']==previous:
            quest=previous
        else:
            cursor=self.cursor_by_actor.get(key,0)%len(self.quest_ids)
            quest=self.quest_ids[cursor];self.cursor_by_actor[key]=(cursor+1)%len(self.quest_ids)
        if now < self.retry.get((key,quest),0):return None
        gear=self.native.strategic_read_gear_snapshot(key,quest)
        phase=self.native.population_read_phase_snapshot()
        if (not isinstance(gear,dict) or gear.get('identity_key')!=key or
            (gear.get('account_id'),gear.get('character_guid'))!=tuple(binding) or gear.get('online') is not True or
            gear.get('control_allowed') is not True or gear.get('reason')!='' or gear.get('level')!=snapshot['level'] or
            phase!=self.catalogue['phase']):
            raise ProtocolError('native seed actor/phase drift')
        rows=[q for q in gear.get('quests',[]) if q.get('quest_id')==quest]
        if len(rows)!=1 or rows[0].get('reason') or rows[0].get('status') not in {'none','incomplete','complete'}:
            self._emit(key,'deferred',quest,'native_seed_quest_not_currently_available');return None
        row=rows[0]
        if row['status']=='none':
            if type(row.get('giver_entry')) is not int or row['giver_entry']==0 or row.get('giver_map')!=gear.get('map_id') or any(type(row.get(n)) not in (float,int) or not math.isfinite(row[n]) for n in ('giver_x','giver_y','giver_z')):
                raise ProtocolError('native seed inactive giver missing')
        candidates=[]
        for reward in row.get('rewards',[]):
            identity=(reward.get('kind'),reward.get('index'))
            if identity not in self.slots[quest] or (reward.get('item',{}).get('entry'),reward.get('item',{}).get('count'))!=self.slots[quest][identity]:
                raise ProtocolError('native seed original reward slot/count drift')
            if reward.get('reason')=='' and reward.get('requirement_result')==0 and reward.get('classification_allowed') is True:
                candidates.append(reward)
        if not candidates:
            self._emit(key,'deferred',quest,'native_seed_no_eligible_reward');return None
        reward=sorted(candidates,key=lambda r:(r['kind']!='choice',r['index'],r['item']['entry']))[0]
        fresh=self._snapshot(key,binding,snapshot['observed_quest_id'])
        if fresh!=snapshot or self.native.strategic_read_gear_snapshot(key,quest)!=gear or self.native.population_read_phase_snapshot()!=phase:
            raise ProtocolError('native seed state drift before accepted handoff')
        self.selected_reward=('guaranteed' if reward['kind']=='fixed' else 'choice',reward['index'],reward['item']['entry'])
        self._capture_event('selected',key,binding,dict(source_id=f"quest:{quest}:{reward['kind']}:{reward['index']}",
            quest_id=quest,kind=reward['kind'],index=reward['index'],item=reward['item']['entry'],phase=phase,
            reward_count=reward['item']['count'],session_incarnation=gear.get('session_incarnation'),
            strategic_generation=gear.get('strategic_generation'),
            catalogue_generation=self.catalogue['observation_generation'],selection_scope='native actor-scoped eligibility; no universal source predicate or dataset acceptance'))
        return quest


class NativeQuestReceiptCapture:
    """One explicitly armed native seed actor/source. Native-only callbacks; original snapshots retained off tick."""
    def __init__(self,native,context,identity,binding,slot,snapshot_id,source_revision,*,in_tick,policy_generation):
        if in_tick():raise ProtocolError('native receipt arm must be off tick')
        self.native=native;self.context=copy.deepcopy(context);self.identity=identity;self.binding=tuple(binding)
        self.slot=copy.deepcopy(slot);self.snapshot_id=snapshot_id;self.source_revision=source_revision
        self.in_tick=in_tick;self.policy_generation=policy_generation
        from capture_native_acquisition import NativeJournal
        self.journal=NativeJournal();self.snapshots=self.journal.entries
        self.capture_records=None
        self.state='armed';self.selection=None;self.selected_now=False;self.observation=None;self.error=None
        self.owner_request=None;self.owner_results=[];self.native_record_id=0
        self.disable_pending=False;self.owner_quiescent=False;self.release_requested=False
        self.drained_descriptor=None
    def fail(self,reason):
        self.error=str(reason)
        if self.state!='finished':self.state='refused'
        if self.owner_request is not None and not self.owner_quiescent:self.disable_pending=True

    def _control(self,enabled):
        if not self.in_tick() or self.owner_request is None:raise ProtocolError('native capture control requires admitted tick and original request')
        request=copy.deepcopy(self.owner_request);request['enabled']=enabled
        # Retain the exact request before any possibly uncertain native effect.
        if enabled:self.disable_pending=True
        result=self.native.strategic_set_quest_acquisition_capture(self.identity,request)
        fields={'status','reason','capture_id','record_id','session_incarnation','strategic_generation'}
        if (not isinstance(result,dict) or set(result)!=fields or result['status'] not in {'armed','disarmed','absent','refused'}
            or not isinstance(result['reason'],str) or result['capture_id']!=request['capture_id']
            or any(type(result[f]) is not int or not 0<=result[f]<=2**64-1 for f in ('record_id','session_incarnation','strategic_generation'))
            or any(result[f]!=request[f] for f in ('session_incarnation','strategic_generation'))):
            raise ProtocolError('native capture control exact result binding')
        if not self.owner_results or self.owner_results[-1]!=result:self.owner_results.append(copy.deepcopy(result))
        record=result['record_id']
        if result['status']=='refused':raise ProtocolError('native capture owner refused: '+result['reason'])
        if enabled and result['status']!='armed':raise ProtocolError('native capture arm not confirmed')
        if not enabled and result['status'] not in {'disarmed','absent'}:raise ProtocolError('native capture disarm not confirmed')
        if result['status']=='absent' and record:raise ProtocolError('native absent owner cannot identify a record')
        if record:
            if self.native_record_id and record!=self.native_record_id:raise ProtocolError('native finite capture record changed')
            self.native_record_id=record
        if enabled:
            self.disable_pending=False
        else:
            self.disable_pending=False;self.owner_quiescent=True
        return result

    def before_strategy_tick(self,bindings,generation):
        if not self.in_tick():raise ProtocolError('native capture cleanup requires admitted tick')
        # Current admission supplies the same durable actor, while false uses the ORIGINAL session/generation.
        if self.disable_pending and bindings.get(self.identity)==self.binding:self._control(False)
    def _fence(self,generation):
        if not self.in_tick():raise ProtocolError('native receipt requires admitted population tick')
        if generation!=self.policy_generation:raise ProtocolError('native receipt source generation changed')
    def selecting(self,key,binding,generation):
        if key!=self.identity or self.state in {'refused','finished'}:return
        self._fence(generation)
        if tuple(binding)!=self.binding:raise ProtocolError('native receipt selected durable identity mismatch')
        if self.state!='awaiting_finalization':self.selected_now=False
    def selected(self,key,binding,selection,generation):
        if key!=self.identity or self.state in {'refused','finished'}:return
        self._fence(generation)
        if tuple(binding)!=self.binding:raise ProtocolError('native receipt selected durable identity mismatch')
        if self.state=='awaiting_finalization':return # Ordinary next/repeated goal cannot rebind pending originals.
        if self.release_requested:return
        reward=self.slot['native_reward']
        expected=(self.slot['source_entry'],reward['kind'],reward['index'],self.slot['item_entry'])
        if tuple(binding)!=self.binding or tuple(selection.get(k) for k in ('quest_id','kind','index','item'))!=expected or selection.get('phase')!=self.context['phase']:
            raise ProtocolError('native receipt selected identity/source/slot/phase mismatch')
        from capture_native_acquisition import _uint
        session=selection.get('session_incarnation');generation=selection.get('strategic_generation')
        _uint(session,2**64-1);_uint(generation,2**64-1)
        if not session or generation!=self.context['observation_generation'] or selection.get('reward_count')!=reward['count']:
            raise ProtocolError('native capture current session/generation/reward-count binding')
        request=dict(enabled=True,capture_id=self.snapshot_id,account_id=self.binding[0],character_guid=self.binding[1],
            session_incarnation=session,strategic_generation=generation,phase_digest=self.context['phase']['policy_digest'],
            quest_id=self.slot['source_entry'],reward_kind='guaranteed' if reward['kind']=='fixed' else 'choice',
            reward_index=reward['index'],item_entry=self.slot['item_entry'],reward_count=reward['count'])
        if self.owner_request is not None and self.owner_request!=request:raise ProtocolError('native capture original owner request changed')
        self.owner_request=request
        self._control(True)  # Validated selected hook runs BEFORE the unchanged five-argument goal call.
        self.selection=copy.deepcopy(selection);self.selected_now=True
    def accepted(self,key,binding,quest,generation):
        if key!=self.identity or self.state in {'refused','finished'}:return
        self._fence(generation)
        if tuple(binding)!=self.binding:raise ProtocolError('native receipt ACK durable identity mismatch')
        if self.state=='awaiting_finalization':return
        if self.release_requested:return
        if not self.selected_now or tuple(binding)!=self.binding or quest!=self.slot['source_entry']:
            raise ProtocolError('native receipt ACK lacks exact current selected source')
        self._control(True)  # Exact idempotent repeat learns the one-shot accepted native record ID.
        if not self.native_record_id:raise ProtocolError('accepted native goal has no newly witnessed capture record')
        self.state='observing'
    def _read(self):
        reward=self.slot['native_reward'];kind='guaranteed' if reward['kind']=='fixed' else 'choice'
        value=self.native.strategic_read_quest_acquisition_snapshot(self.identity,self.slot['source_entry'],kind,reward['index'],self.slot['item_entry'])
        if not isinstance(value,dict) or value.get('schema')!='core1-native-quest-acquisition-v1':raise ProtocolError('native acquisition snapshot schema')
        self.journal.append(value)
        if value.get('valid') is not True:raise ProtocolError('native acquisition snapshot refused: '+str(value.get('reason')))
        if value.get('identity_key')!=self.identity or (value.get('account_id'),value.get('character_guid'))!=self.binding:raise ProtocolError('native acquisition snapshot durable identity drift')
    def tick(self,generation):
        self._fence(generation)
        if self.state=='finished' and not self.release_requested and not self.disable_pending:return
        if self.disable_pending:return # Only fresh admitted bindings in before_strategy_tick authorize cleanup.
        if self.state not in {'observing','awaiting_finalization'} and not self.release_requested and not self.native_record_id:return
        if self.observation is None and self.state=='observing':
            from capture_native_acquisition import capture_current_observations
            self.observation=capture_current_observations(self.native,self.context,self.identity,self.slot['source_entry'],self.snapshot_id,self.source_revision)
        self._read()
        self._latch_records()
        if (self.state=='awaiting_finalization' or self.release_requested) and self._finalized():self.state='finished'
    def _latch_records(self):
        if self.capture_records is None:
            fields=('identity_key','account_id','character_guid','session_incarnation','strategic_generation','goal_revision','phase_digest','source_key','quest_id','reward_kind','reward_index','item_entry','reward_count')
            record=self.journal.records.get(self.native_record_id)
            if record is not None:
                if self.owner_request is not None and (record['identity_key']!=self.identity or any(record[field]!=self.owner_request[field] for field in ('account_id','character_guid','session_incarnation','strategic_generation','phase_digest','quest_id','reward_kind','reward_index','item_entry','reward_count'))):
                    raise ProtocolError('native journal/control original owner mismatch')
                self.capture_records={self.native_record_id:{field:copy.deepcopy(record[field]) for field in fields}}
    def _finalized(self):
        if not self.capture_records:return False
        for identity,binding in self.capture_records.items():
            record=self.journal.records.get(identity)
            if record is None or any(record.get(field)!=value for field,value in binding.items()):
                raise ProtocolError('native original finalization owner binding changed')
            if record.get('terminal') is not True or not isinstance(record.get('record_sha256'),str) or len(record['record_sha256'])!=64:return False
        return True
    def rewarded(self,key,binding,quest,generation):
        if key!=self.identity or self.state in {'refused','finished'}:return
        self._fence(generation)
        if tuple(binding)!=self.binding:raise ProtocolError('native acquisition terminal durable identity drift')
        if self.state=='awaiting_finalization':return
        if quest!=self.slot['source_entry'] or self.state!='observing':raise ProtocolError('native acquisition terminal association drift')
        self._read();self._latch_records();self.state='finished' if self._finalized() else 'awaiting_finalization'
    def lost(self,key,quest,generation):
        if key!=self.identity or self.state in {'refused','finished'}:return
        self._fence(generation)
        if self.disable_pending:return # Loss callbacks can come from excluded bindings; retain originals without native reads.
        if self.state=='awaiting_finalization':return # Original completion ownership outlives ordinary goal changes.
        if self.state=='observing':
            self._read();self._latch_records()
            if self._finalized():self.state='finished';return
        self.fail('native seed goal lost/admission suspended/reset before valid terminal')
    @classmethod
    def recover(cls,native,original_descriptor,original_root,*,in_tick,policy_generation):
        if in_tick():raise ProtocolError('native receipt recovery must be off tick')
        import json
        from pathlib import Path
        from capture_native_acquisition import read_descriptor,native_observations
        original=json.loads(read_descriptor(original_descriptor,Path(original_root)))
        data=original['data']
        if original.get('kind')!='native_execution' or data.get('schema') not in {'core1-original-native-quest-observations-v1','core1-original-native-quest-observations-v2'}:raise ProtocolError('native receipt recovery original type')
        value=cls(native,data['context'],data['identity_key'],data['binding'],data['native_slot'],original['snapshot_id'],original['source_revision'],in_tick=in_tick,policy_generation=policy_generation)
        value.selection=copy.deepcopy(data['selection'])
        for snapshot in native_observations(data):value.journal.append(snapshot)
        value.capture_records={int(key):copy.deepcopy(binding) for key,binding in data['capture_records'].items()} if data.get('capture_records') is not None else None
        owner=data.get('native_capture_owner')
        if owner is not None:
            request=owner['request'];fields={'enabled','capture_id','account_id','character_guid','session_incarnation','strategic_generation','phase_digest','quest_id','reward_kind','reward_index','item_entry','reward_count'}
            if (not isinstance(request,dict) or set(request)!=fields or request['enabled'] is not True
                or request['capture_id']!=original['snapshot_id'] or (request['account_id'],request['character_guid'])!=value.binding):
                raise ProtocolError('native recovery original capture owner binding')
            from capture_native_acquisition import _uint
            for field in ('account_id','quest_id','reward_index','item_entry','reward_count'):_uint(request[field])
            for field in ('character_guid','session_incarnation','strategic_generation'):_uint(request[field],2**64-1)
            reward=value.slot['native_reward'];kind='guaranteed' if reward['kind']=='fixed' else 'choice'
            if (not request['session_incarnation'] or request['strategic_generation']!=value.context['observation_generation']
                or request['phase_digest']!=value.context['phase']['policy_digest']
                or (request['quest_id'],request['reward_kind'],request['reward_index'],request['item_entry'],request['reward_count'])!=(value.slot['source_entry'],kind,reward['index'],value.slot['item_entry'],reward['count'])):
                raise ProtocolError('native recovery original source/session owner mismatch')
            _uint(owner['record_id'],2**64-1)
            if not isinstance(owner['results'],list):raise ProtocolError('native recovery original control results')
            for result in owner['results']:
                if (not isinstance(result,dict) or set(result)!={'status','reason','capture_id','record_id','session_incarnation','strategic_generation'}
                    or result['status'] not in {'armed','disarmed','absent','refused'} or not isinstance(result['reason'],str)
                    or any(result[field]!=request[field] for field in ('capture_id','session_incarnation','strategic_generation'))):
                    raise ProtocolError('native recovery original control result mismatch')
                for field in ('record_id','session_incarnation','strategic_generation'):_uint(result[field],2**64-1)
            returned_ids={result['record_id'] for result in owner['results'] if result['record_id'] and result['status'] in {'armed','disarmed'}}
            if owner['record_id'] and owner['record_id'] not in returned_ids:
                raise ProtocolError('native recovery control/record owner mismatch')
            value.owner_request=copy.deepcopy(request);value.owner_results=copy.deepcopy(owner['results'])
            value.native_record_id=owner['record_id'];value.disable_pending=True
            if not value.capture_records:value.capture_records=None  # Serialized unknown scope remains latchable after authoritative control.
            elif not value.native_record_id or set(value.capture_records)!={value.native_record_id}:
                raise ProtocolError('native recovery finite record owner mismatch')
            # Recovery NEVER enables a registration. Original false is retried under current same-owner admission.
            if value.native_record_id:
                record=value.journal.records.get(value.native_record_id)
                if record is not None and any(record[field]!=request[field] for field in ('account_id','character_guid','session_incarnation','strategic_generation','phase_digest','quest_id','reward_kind','reward_index','item_entry','reward_count')):
                    raise ProtocolError('native recovery journal/owner association mismatch')
        else:
            if value.capture_records is None:
                fields=('identity_key','account_id','character_guid','session_incarnation','strategic_generation','goal_revision','phase_digest','source_key','quest_id','reward_kind','reward_index','item_entry','reward_count')
                value.capture_records={identity:{field:copy.deepcopy(record[field]) for field in fields} for identity,record in value.journal.records.items()}
            if len(value.capture_records)==1:value.native_record_id=next(iter(value.capture_records))
            # Legacy original custody only; no guessed registration or new arm.
        value._latch_records()
        value.observation=copy.deepcopy(data['actor_observation']);value.state='awaiting_finalization'
        value.recovered_original=copy.deepcopy(original_descriptor)
        return value

    def _document(self):
        return dict(snapshot_id=self.snapshot_id,kind='native_execution',source_revision=self.source_revision,
            data=dict(schema='core1-original-native-quest-observations-v2',identity_key=self.identity,binding=list(self.binding),
                native_slot=self.slot,context=self.context,selection=self.selection,observations=self.snapshots,
                capture_records={} if self.owner_request is not None and self.capture_records is None else self.capture_records,
                actor_observation=self.observation,state=self.state,error=self.error,ready=False,
                native_capture_owner=dict(request=self.owner_request,results=self.owner_results,record_id=self.native_record_id,
                    disable_pending=self.disable_pending,quiescent=self.owner_quiescent,release_requested=self.release_requested) if self.owner_request is not None else None,
                recovered_original=getattr(self,'recovered_original',None)))

    def request_release(self):
        """OFF TICK: queue disarm; only drop the Python observer after quiescence and current durable custody."""
        if self.in_tick():raise ProtocolError('native capture release must be off tick')
        self.release_requested=True
        if self.owner_request is not None and not self.owner_quiescent:
            self.disable_pending=True
            return dict(released=False,native_owner='disable_pending',journal_release=False)
        if (self.native_record_id or self.capture_records) and not self._finalized():
            return dict(released=False,native_owner='awaiting_original_finalization',journal_release=False)
        if self.owner_request is None and not self.native_record_id and not self.capture_records and not self.journal.records:
            return dict(released=True,native_owner='never_armed',journal_release=False)
        from pathlib import Path
        from capture_native_acquisition import canonical,read_descriptor
        if self.drained_descriptor is None:
            return dict(released=False,native_owner='drain_current_original_required',journal_release=False)
        current=canonical(self._document())+b'\n'
        if read_descriptor(self.drained_descriptor,Path('.'))!=current:
            return dict(released=False,native_owner='drain_current_original_required',journal_release=False)
        return dict(released=True,native_owner=self.owner_results[-1]['status'] if self.owner_results else 'legacy_original_only',journal_release=False)

    def drain(self,output):
        if self.in_tick():raise ProtocolError('native receipt drain must be off tick')
        from capture_native_acquisition import atomic_file,canonical
        # One atomic original envelope: no partial semantic documents or silent event truncation.
        value=self._document()
        raw=canonical(value)+b'\n';atomic_file(output,raw)
        import hashlib
        from pathlib import Path
        from capture_native_acquisition import read_descriptor
        descriptor=dict(path=str(Path(output).absolute()),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),generation=self.snapshot_id,ready=False)
        if read_descriptor(descriptor,Path('.'))!=raw:raise ProtocolError('native original durable readback mismatch')
        self.drained_descriptor=copy.deepcopy(descriptor)
        return descriptor
