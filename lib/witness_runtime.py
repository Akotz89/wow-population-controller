"""Finite selected-admitted-actor adapter; native RunPopulationTick owns authority."""
import copy
import hashlib
import time
from pathlib import Path

from gear_policy import GearInputs, GearPolicy, _file, _json, _sha
from quest_policy import ProtocolError
from native_seed_policy import NativeSeedPolicy, NativeQuestReceiptCapture
from native_custody_ack import NativeCustodyAckQueue, hex64
from capture_native_acquisition import catalogue, decode, prepare_current_observation


def require(value, reason):
    if not value:
        raise ProtocolError(reason)


def integer(value, maximum=2**64-1):
    return type(value) is int and 0 < value <= maximum


def descriptor_bytes(value):
    require(isinstance(value, dict) and set(value) == {'path', 'sha256'}, 'exact physical descriptor required')
    return _file(value['path'], value['sha256'], 128*1024*1024)


def load_config(path, sha256):
    value = _json(_file(path, sha256, 65536))
    fields = {'schema', 'identity_key', 'account_id', 'character_guid', 'account_type',
              'effective_ceiling', 'scheduler_eligible', 'phase', 'strategic_generation',
              'snapshot_id', 'exporter_revision', 'seed_catalogue', 'dependencies'}
    require(isinstance(value, dict) and set(value) == fields and value['schema'] == 'cpi225-selected-native-witness-v1',
            'witness physical config schema')
    _sha(value['identity_key']); _sha(value['exporter_revision'])
    require(integer(value['account_id'], 2**32-1) and value['account_id'] != 142 and
            integer(value['character_guid'], 2**32-1) and type(value['account_type']) is int and
            value['account_type'] in (1, 2), 'selected actor must be existing autonomous RNDBOT, excluding account142')
    require(integer(value['effective_ceiling'], 80) and type(value['scheduler_eligible']) is bool and
            integer(value['strategic_generation']) and isinstance(value['snapshot_id'], str) and value['snapshot_id'],
            'witness accepted ceiling/scheduler/generation/snapshot')
    phase = value['phase']
    require(isinstance(phase, dict) and set(phase) == {'enabled', 'policy_valid', 'phase_id', 'level_ceiling', 'policy_digest'} and
            phase['enabled'] is True and phase['policy_valid'] is True and integer(phase['level_ceiling'], 80) and
            isinstance(phase['phase_id'], str) and phase['phase_id'] and value['effective_ceiling'] <= phase['level_ceiling'],
            'witness accepted phase')
    require(hex64(phase['policy_digest']), 'witness accepted native phase digest')
    return value


class SelectedNative:
    """Only the existing selected actor ports, with a fresh check before every effect."""
    keyed = {'strategic_read_quest_snapshot', 'strategic_read_gear_snapshot', 'strategic_read_gear_resources',
             'strategic_read_source_predicates', 'strategic_read_vendor_offers', 'strategic_set_quest_goal',
             'strategic_buy_vendor_item', 'strategic_set_quest_acquisition_capture',
             'strategic_read_quest_acquisition_snapshot', 'strategic_ack_quest_acquisition_records'}
    effects = {'strategic_set_quest_goal', 'strategic_buy_vendor_item', 'strategic_set_quest_acquisition_capture',
               'strategic_ack_quest_acquisition_records', 'strategic_renew_quest_goals'}

    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        require(name in self.keyed | {'strategic_renew_quest_goals', 'population_read_phase_snapshot'},
                'native witness port is outside selected scope: ' + name)
        def call(*args):
            owner = self.owner
            owner.require_tick()
            if name == 'population_read_phase_snapshot':
                return owner.phase()
            if name == 'strategic_renew_quest_goals':
                require(len(args) == 1 and isinstance(args[0], list) and len(args[0]) <= 1 and
                        all(row.get('identity_key') == owner.key for row in args[0]), 'foreign renewal binding')
            else:
                require(args and args[0] == owner.key, 'foreign native actor binding')
            owner.check_source()
            owner.phase()
            if name in self.effects:
                cleanup = name == 'strategic_ack_quest_acquisition_records' or (
                    name == 'strategic_set_quest_acquisition_capture' and args[1].get('enabled') is False)
                owner.actor_context(control=not cleanup)
                if name == 'strategic_set_quest_goal' and isinstance(owner.strategy, NativeSeedPolicy):
                    require(owner.target_slot is not None, 'seed target is missing')
                    slot = owner.target_slot; reward = slot['native_reward']
                    expected = (owner.key, slot['source_entry'], 'guaranteed' if reward['kind'] == 'fixed' else 'choice',
                                reward['index'], slot['item_entry'])
                    require(args == expected, 'explicit slot differs from native seed reward preference')
                    observer = owner.strategy.acquisition_capture
                    require(observer is None or (observer.error is None and observer.selected_now), 'finite capture selection refused')
                if name == 'strategic_set_quest_acquisition_capture':
                    request = args[1]
                    require((request.get('account_id'), request.get('character_guid')) == owner.binding and
                            request.get('phase_digest') == owner.config['phase']['policy_digest'], 'capture owner drift')
                if name == 'strategic_ack_quest_acquisition_records':
                    owner.validate_ack(owner.ack)
            value = getattr(owner.raw, name)(*args)
            if name in {'strategic_read_gear_snapshot', 'strategic_read_quest_snapshot', 'strategic_read_gear_resources'}:
                require(isinstance(value, dict) and value.get('identity_key') == owner.key and
                        (value.get('account_id'), value.get('character_guid')) == owner.binding, 'native actor binding drift')
            return value
        return call


class Witness:
    def __init__(self, native, config, *, audit=lambda row: None, clock=time.time):
        self.raw, self.config = native, copy.deepcopy(config)
        self.key = config['identity_key']
        self.binding = (config['account_id'], config['character_guid'])
        self.clock = clock
        self._active = False
        self.native = SelectedNative(self)
        self.mode = 'observe'
        self.session_command = None
        self.strategy = None
        self.ack = None
        self.required_acks, self.applied_acks = set(), set()
        self.session = None
        self.relog_before = None
        self.relog_state = None
        self.status = {'mode': 'observe', 'reason': 'awaiting_explicit_command'}
        self.audit = audit
        self.seed_raw = descriptor_bytes(config['seed_catalogue'])
        self.seed = decode(self.seed_raw)
        self.slots = catalogue(self.seed)
        require(self.seed['phase'] == config['phase'] and self.seed['observation_generation'] == config['strategic_generation'],
                'original catalogue phase/source generation drift')
        self.seed_sha = hashlib.sha256(self.seed_raw).hexdigest()
        self.strategy = NativeSeedPolicy(self.native, audit, prepared_catalogue=self.seed,
            source_sha256=self.seed_sha, max_per_tick=1, renewal_budget=1)
        self.source_sha = self.seed_sha
        self.target_slot = None
        self._capture_context = None

    def off_tick(self):
        require(not self._active, 'command requires off tick')

    def require_tick(self):
        require(self._active, 'actor API requires population callback')
        # This local marker prevents accidental off-tick use. The native Provider remains the authority.

    def phase(self):
        self.require_tick()
        phase = self.raw.population_read_phase_snapshot()
        require(phase == self.config['phase'], 'native witness phase drift')
        return phase

    def check_source(self):
        require(self.strategy.source_sha256 == self.source_sha, 'witness strategy source drift')
        require(self.strategy.native is self.native, 'witness strategy native boundary drift')

    def earned(self):
        self.require_tick()
        rows = self.raw.population_read_earned_snapshot([self.key])
        require(isinstance(rows, list) and len(rows) == 1, 'selected admitted actor missing')
        row = rows[0]
        require(row.get('identity_key') == self.key and row.get('character_guid') == self.binding[1] and
                integer(row.get('level'), 80) and type(row.get('online')) is bool and
                type(row.get('session_requested')) is bool, 'selected earned binding drift')
        return row

    def actor_context(self, *, control=True):
        self.require_tick(); self.check_source(); self.phase()
        row = self.earned()
        require(row['online'], 'selected actor is offline')
        gear = self.raw.strategic_read_gear_snapshot(self.key, 0)
        require(isinstance(gear, dict) and gear.get('identity_key') == self.key and
                (gear.get('account_id'), gear.get('character_guid')) == self.binding and gear.get('online') is True,
                'selected actor context binding drift')
        require(gear.get('strategic_generation') == self.config['strategic_generation'] and
                integer(gear.get('session_incarnation')), 'selected actor context source/session drift')
        if self.session is None:
            self.session = gear['session_incarnation']
        require(gear['session_incarnation'] == self.session, 'unexpected selected session changed')
        require(not control or row['level'] < self.config['effective_ceiling'], 'accepted policy holds selected actor')
        require(not control or (gear.get('control_allowed') is True and gear.get('reason') == ''),
                'native control deferred: ' + str(gear.get('reason')))
        return gear

    def command(self, mode):
        self.off_tick()
        require(mode in {'observe', 'seed', 'capture', 'disarm', 'release', 'relog', 'gear'}, 'unsupported witness mode')
        if mode in {'release', 'relog'}:
            require(self.strategy.acquisition_capture is None and self.ack is None and
                    self.required_acks <= self.applied_acks, 'capture originals/quiescence/ACK must finish before session release')
            require(self.session is not None and self.status.get('gear') is not None, 'observed session/gear required before release')
            self.relog_before = copy.deepcopy(self.status['gear'])
            self.session_command = mode
            self.relog_state = 'release_pending'
            self.mode = 'observe'
        elif mode == 'disarm':
            return self.release_capture()
        else:
            require(mode != 'gear' or isinstance(self.strategy, GearPolicy), 'accepted gear binding must be staged off tick')
            require(mode not in {'seed', 'capture'} or isinstance(self.strategy, NativeSeedPolicy), 'seed source is not active')
            require(mode not in {'seed', 'capture'} or self.target_slot is not None, 'explicit original seed target required')
            require(mode != 'capture' or self.strategy.acquisition_capture is not None, 'finite capture must be armed')
            self.mode = mode
        return {'queued': mode, 'native_effect': False}

    def request_session(self):
        self.off_tick()
        require(self.relog_state is None, 'session lifecycle already pending')
        self.session_command = 'request'
        return {'queued': 'request', 'native_effect': False}

    def select_seed_slot(self, slot_key):
        self.off_tick()
        require(isinstance(self.strategy, NativeSeedPolicy) and self.strategy.acquisition_capture is None,
                'seed target requires seed mode and no retained capture')
        rows = [slot for slot in self.slots if slot['key'] == slot_key]
        require(len(rows) == 1, 'exact original seed slot missing')
        self.target_slot = copy.deepcopy(rows[0])
        # Keep the complete immutable catalogue; only this finite strategy's cursor is scoped.
        self.strategy.quest_ids = (self.target_slot['source_entry'],)
        self.strategy.cursor_by_actor.clear()
        return copy.deepcopy(self.target_slot)

    def arm(self, identity_key, binding, exact_slot_key, snapshot_id, exporter_revision):
        self.off_tick()
        require(identity_key == self.key and tuple(binding) == self.binding and snapshot_id == self.config['snapshot_id'] and
                exporter_revision == self.config['exporter_revision'], 'capture physical actor/snapshot/exporter drift')
        require(self.strategy.acquisition_capture is None, 'finite capture retained')
        observed = self.status.get('gear')
        require(self.status.get('held') is not True and
                (observed is None or (observed.get('control_allowed') is True and observed.get('reason') == '')),
                'new capture requires accepted progression and native control')
        self.select_seed_slot(exact_slot_key)
        slot = self.target_slot
        context = prepare_current_observation(self.seed, slot['source_entry'])
        self._capture_context = copy.deepcopy(context)
        self.strategy.acquisition_capture = NativeQuestReceiptCapture(self.native, context, self.key, self.binding,
            slot, snapshot_id, exporter_revision, in_tick=lambda: self._active, policy_generation=self.source_sha)
        return {'armed': True, 'ready': False}

    def validate_observer(self, observer):
        expected = self._capture_context if self._active else prepare_current_observation(self.seed, observer.slot['source_entry'])
        require(observer.context == expected and observer.slot in self.slots, 'capture original catalogue/context drift')
        require(observer.identity == self.key and observer.binding == self.binding and observer.policy_generation == self.source_sha and
                observer.snapshot_id == self.config['snapshot_id'] and observer.source_revision == self.config['exporter_revision'] and
                observer.context['phase'] == self.config['phase'] and
                observer.context['observation_generation'] == self.config['strategic_generation'], 'retained capture owner/source drift')

    def recover_capture(self, descriptor, root):
        self.off_tick()
        require(self.strategy.acquisition_capture is None and isinstance(self.strategy, NativeSeedPolicy), 'capture recovery requires empty seed capture')
        observer = NativeQuestReceiptCapture.recover(self.native, descriptor, root, in_tick=lambda: self._active,
            policy_generation=self.source_sha)
        self.validate_observer(observer)
        self._capture_context = copy.deepcopy(observer.context)
        self.strategy.acquisition_capture = observer
        self.mode = 'observe'
        return {'recovering': True, 'ready': False}

    def drain(self, output):
        self.off_tick()
        observer = self.strategy.acquisition_capture
        require(observer is not None, 'no retained capture')
        self.validate_observer(observer)
        return observer.drain(output)

    def release_capture(self):
        self.off_tick()
        observer = self.strategy.acquisition_capture
        require(observer is not None, 'no retained capture')
        self.validate_observer(observer)
        self.mode = 'observe'
        result = observer.request_release()
        if result['released']:
            self.required_acks.update((record_id, observer.journal.records[record_id]['record_sha256'])
                                      for record_id in (observer.capture_records or {}))
            self.strategy.acquisition_capture = None
        return result

    def validate_ack(self, queue):
        require(queue is not None and queue.document['identity_key'] == self.key and
                tuple(queue.document['binding']) == self.binding and queue.document['snapshot_id'] == self.config['snapshot_id'],
                'native ACK selected custody binding drift')
        for row in queue.document['records']:
            ack = row['ack']
            require((ack['account_id'], ack['character_guid']) == self.binding and
                    ack['phase_digest'] == self.config['phase']['policy_digest'] and
                    ack['strategic_generation'] == self.config['strategic_generation'], 'native ACK phase/source binding drift')

    def prepare_ack(self, descriptor, root, output):
        self.off_tick(); require(self.ack is None and self.strategy.acquisition_capture is None, 'native ACK requires released capture and no retained queue')
        # Validate selected original before creating a durable queue for it.
        observer = NativeQuestReceiptCapture.recover(self.native, descriptor, root, in_tick=lambda: self._active,
            policy_generation=self.source_sha)
        self.validate_observer(observer)
        queue = NativeCustodyAckQueue.prepare(descriptor, root, output, in_tick=lambda: self._active)
        self.validate_ack(queue); self.ack = queue
        return {'queued': len(queue.pending()), 'native_release': False}

    def recover_ack(self, path):
        self.off_tick(); require(self.ack is None and self.strategy.acquisition_capture is None, 'native ACK requires released capture and no retained queue')
        queue = NativeCustodyAckQueue.recover(path, in_tick=lambda: self._active)
        self.validate_ack(queue)
        observer = NativeQuestReceiptCapture.recover(self.native, queue.document['original_descriptor'], queue.document['original_root'],
            in_tick=lambda: self._active, policy_generation=self.source_sha)
        self.validate_observer(observer); self.ack = queue
        return {'queued': len(queue.pending()), 'native_release': False}

    def settle_ack(self):
        self.off_tick(); self.validate_ack(self.ack)
        result = self.ack.settle()
        self.applied_acks.update((row['ack']['record_id'], row['ack']['record_sha256']) for row in self.ack.document['records']
                                 if row['status'] == 'applied')
        require(not any(row['status'] == 'refused' for row in self.ack.document['records']), 'native custody ACK refused; retain queue')
        if not self.ack.pending():
            self.ack = None
        return result

    def stage_gear(self, path, sha256):
        self.off_tick()
        require(self.strategy.acquisition_capture is None and self.ack is None and self.required_acks <= self.applied_acks,
                'gear switch requires finalized retained originals and settled ACK')
        inputs = GearInputs.from_binding(path, sha256)
        actor = inputs.profile['actors'].get(self.key)
        require(actor is not None and (actor.get('account_id'), actor.get('character_guid')) == self.binding,
                'accepted gear profile selected binding mismatch')
        require(inputs.profile['phase']['phase_id'] == self.config['phase']['phase_id'] and
                inputs.profile['phase']['policy_digest'] == self.config['phase']['policy_digest'] and
                inputs.profile['phase']['level_ceiling'] == self.config['phase']['level_ceiling'], 'accepted gear profile phase mismatch')
        self.strategy = GearPolicy(self.native, self.audit, inputs=inputs, source_sha256=inputs.binding_sha256,
                                   max_per_tick=1, renewal_budget=1)
        self.source_sha = inputs.binding_sha256
        self.mode = 'observe'
        return {'staged': 'gear', 'native_effect': False}

    def _capacity(self):
        self.phase()
        value = self.raw.population_read_capacity_snapshot()
        require(isinstance(value, dict) and value.get('valid') is True and integer(value.get('generation')) and
                type(value.get('observed_epoch')) is int and 0 <= self.clock()-value['observed_epoch'] <= 30 and
                type(value.get('available_sessions')) is int and value['available_sessions'] > 0 and
                type(value.get('online_limit')) is int and value['online_limit'] > 0, 'fresh native capacity unavailable')
        return value

    def tick(self, diff_ms):
        require(not self._active, 'recursive witness callback')
        self._active = True
        try:
            self.check_source(); self.phase()
            row = self.earned()
            observer = self.strategy.acquisition_capture
            if observer is not None:
                self.validate_observer(observer)
            if self.ack is not None:
                self.validate_ack(self.ack)
            # Account/session context is checked before refreshing online actor permissions.
            if row['online']:
                raw = self.raw.strategic_read_gear_snapshot(self.key, 0)
                require(raw.get('identity_key') == self.key and (raw.get('account_id'), raw.get('character_guid')) == self.binding and
                        raw.get('online') is True, 'selected online actor binding drift')
                if raw.get('control_allowed') is True or raw.get('session_incarnation') or raw.get('strategic_generation'):
                    require(raw.get('strategic_generation') == self.config['strategic_generation'] and integer(raw.get('session_incarnation')),
                            'selected online actor source/session drift')
                    if self.session is None:
                        self.session = raw['session_incarnation']
                    require(raw['session_incarnation'] == self.session, 'unexpected selected session changed')
                if raw.get('control_allowed') is not True:
                    # Legacy held snapshots may omit metadata; retained cleanup still requires full context below.
                    require(raw.get('reason') == 'progression_held' or
                            (observer is not None or self.ack is not None or self.session_command in {'release', 'relog'}),
                            'native control deferred: ' + str(raw.get('reason')))
            held = row['level'] >= self.config['effective_ceiling']
            desired = row['session_requested'] or self.session_command in {'request', 'relog'}
            eligible = self.config['scheduler_eligible'] and desired and not held
            require(self.raw.population_apply_progression_policy([dict(identity_key=self.key,
                effective_ceiling=self.config['effective_ceiling'], held=held, scheduler_eligible=eligible)]) is True,
                'selected native policy refresh refused')
            self.status = {'mode': self.mode, 'earned': copy.deepcopy(row), 'held': held, 'scheduler_eligible': eligible}
            if self.session_command in {'release', 'relog'} and self.relog_state == 'release_pending':
                require(observer is None and self.ack is None and self.required_acks <= self.applied_acks, 'release custody changed')
                self.actor_context(control=False); self.phase()
                if self.raw.population_release_session(self.key) is True:
                    self.relog_state = 'awaiting_offline'
                self.status['session_lifecycle'] = self.relog_state
                return True
            if self.relog_state == 'awaiting_offline':
                if row['online'] or row['session_requested']:
                    self.status['session_lifecycle'] = self.relog_state
                    return True
                self.session = None
                if self.session_command == 'release':
                    self.session_command = None; self.relog_state = None
                    self.status['session_lifecycle'] = 'offline_observed'
                    return True
                self.relog_state = 'request_pending'
            if self.session_command == 'request' or self.relog_state == 'request_pending':
                maintenance = self.session_command == 'relog' and self.relog_state == 'request_pending'
                if maintenance:
                    require(self.relog_before is not None and self.session is None and
                            not row['online'] and not row['session_requested'] and observer is None and self.ack is None and
                            self.required_acks <= self.applied_acks, 'relog requires released original session, offline and complete custody')
                else:
                    require(not held and self.config['scheduler_eligible'], 'accepted policy holds selected session')
                capacity = self._capacity(); fresh = self.earned(); self.phase()
                require((not fresh['online'] and not fresh['session_requested']) if maintenance else
                        fresh['level'] < self.config['effective_ceiling'], 'selected state changed before session request')
                if self.raw.population_request_session(self.key, capacity['generation']) is True:
                    if self.session_command == 'request':
                        self.session_command = None
                    else:
                        self.relog_state = 'awaiting_online'
                self.status['session_lifecycle'] = self.relog_state or 'request_queued'
                return True
            if not row['online']:
                self.status['reason'] = 'selected_actor_offline'
                return True
            gear = self.actor_context(control=False)
            self.status['gear'] = copy.deepcopy(gear)
            if self.relog_state == 'awaiting_online':
                require(gear['session_incarnation'] != self.relog_before['session_incarnation'], 'relog session did not change')
                self.status['relog_equipment_equal'] = gear['equipment'] == self.relog_before['equipment']
                self.status['before_relog_equipment'] = copy.deepcopy(self.relog_before['equipment'])
                self.status['session_lifecycle'] = 'online_new_session_observed'
                self.session_command = None; self.relog_state = None
            if observer is not None:
                observer.before_strategy_tick({self.key: self.binding}, self.source_sha)
            controlled = gear.get('control_allowed') is True and gear.get('reason') == ''
            if eligible and controlled and self.mode in {'seed', 'capture', 'gear'} and not (observer and observer.release_requested):
                self.strategy.tick({self.key: self.binding})
                # Exact desired slot must match NativeSeedPolicy's own native reward preference.
                if self.target_slot is not None and isinstance(self.strategy, NativeSeedPolicy) and self.strategy.selected_reward is not None:
                    reward = self.target_slot['native_reward']
                    expected = ('guaranteed' if reward['kind'] == 'fixed' else 'choice', reward['index'], self.target_slot['item_entry'])
                    require(self.strategy.selected_reward == expected, 'explicit slot differs from native seed reward preference')
            if observer is not None:
                observer.tick(self.source_sha)
            if self.ack is not None:
                self.ack.apply(self.native)
            self.status['reason'] = '' if controlled else 'native control deferred: ' + str(gear.get('reason'))
            return True
        except Exception as error:
            # Refusal must not trigger RunPopulationTick's global FailClosed side effect.
            self.status['reason'] = type(error).__name__ + ': ' + str(error)
            return True
        finally:
            self._active = False
