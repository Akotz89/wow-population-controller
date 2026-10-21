"""2,500-row target with explicit admission holds; fresh identities admit at level one."""
import sys
import time
from pathlib import Path
import azerothcore
root = Path(str(globals()['__script_root__'])).resolve(strict=True).parent
library = root / 'lib'
if str(library) not in sys.path:
    sys.path.insert(0, str(library))
# Bind the executing bootstrap and source bytes before refreshing this runtime's imports.
import hashlib as _source_hashlib
import os as _source_os
import stat as _source_stat

def _source_bytes(path):
    descriptor = _source_os.open(path, _source_os.O_RDONLY | _source_os.O_NOFOLLOW)
    with _source_os.fdopen(descriptor, 'rb') as stream:
        if not _source_stat.S_ISREG(_source_os.fstat(stream.fileno()).st_mode):
            raise ValueError('nonregular population source')
        return stream.read()

def _snapshot_population_sources():
    if library.is_symlink() or not library.is_dir():
        raise ValueError('invalid population library root')
    if any(path.is_symlink() for path in library.rglob('*')):
        raise ValueError('symlink in population library')
    return tuple((str(path), _source_hashlib.sha256(_source_bytes(path)).hexdigest())
                 for path in sorted(library.rglob('*.py')) + [Path(__file__)])

_bootstrap_bytes = _source_bytes(Path(__file__))
_frame_code = sys._getframe().f_code
if _frame_code != compile(_bootstrap_bytes, str(__file__), 'exec',
                          flags=_frame_code.co_flags, dont_inherit=True):
    raise ValueError('executed bootstrap differs from its source')
_population_source_binding = _snapshot_population_sources()
_prior_controller = (globals().get('__previous_population_namespace__') or {}).get('controller')
_prior_source_binding = getattr(_prior_controller, '_policy_source_contract', None)
if _prior_source_binding and _prior_source_binding != _population_source_binding:
    raise ValueError('population source changed; policy-only reload requires the same source generation')
# Refresh the package shell to protect accepted parent attributes on rejected imports.
# Shared models remain cached only when the preceding exact source proof is available.
_reload_modules = {'population_runtime','population_runtime.config','population_runtime.controller',
                   'population_runtime.state','policy_continuity'}
for _name, _module in tuple(sys.modules.items()):
    if _name == 'population_runtime' or _name.startswith('population_runtime.') or _name in {'policy_continuity','quest_policy','gear_policy','capture_native_acquisition','ingest_capture','native_seed_policy','native_custody_ack'}:
        _origin = getattr(_module, '__file__', None)
        if not _origin or not Path(_origin).resolve().is_relative_to(library):
            raise RuntimeError('population module belongs to another source root: ' + _name)
        if _name in _reload_modules or not _prior_source_binding:
            del sys.modules[_name]
# Compile source bytes directly, including first-load shared modules; never trust stale pyc.
import importlib.machinery as _machinery
class _PopulationSourceLoader(_machinery.SourceFileLoader):
    def get_code(self, fullname):
        filename = self.get_filename(fullname)
        data = _source_bytes(Path(filename))
        if _source_hashlib.sha256(data).hexdigest() != dict(_population_source_binding).get(filename):
            raise ValueError('population source changed during import')
        return self.source_to_code(data, filename)
class _PopulationSourceFinder:
    _population_source_finder = True
    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == 'population_runtime' or fullname.startswith('population_runtime.') or fullname in {'policy_continuity','quest_policy','gear_policy','capture_native_acquisition','ingest_capture','native_seed_policy','native_custody_ack'}):
            return None
        spec = _machinery.PathFinder.find_spec(fullname, path, target)
        if not spec or not spec.origin or not Path(spec.origin).resolve().is_relative_to(library):
            raise ImportError('population source outside its library: ' + fullname)
        spec.loader = _PopulationSourceLoader(fullname, spec.origin)
        return spec
# Cached module loaders must not retain this active bootstrap namespace.
import types as _source_types
_source_read_globals = {'__builtins__': __builtins__, '_source_os': _source_os, '_source_stat': _source_stat}
_loader_source_bytes = _source_types.FunctionType(_source_bytes.__code__, _source_read_globals)
_source_import_globals = {'__builtins__': __builtins__, 'Path': Path, 'library': library,
    '_source_bytes': _loader_source_bytes, '_source_hashlib': _source_hashlib,
    '_population_source_binding': _population_source_binding, '_machinery': _machinery,
    '_PopulationSourceLoader': _PopulationSourceLoader}
for _source_class, _source_method_name in ((_PopulationSourceLoader, 'get_code'), (_PopulationSourceFinder, 'find_spec')):
    _source_method = getattr(_source_class, _source_method_name)
    setattr(_source_class, _source_method_name, _source_types.FunctionType(
        _source_method.__code__, _source_import_globals, _source_method.__name__, _source_method.__defaults__))
del _source_class, _source_method_name, _source_method
sys.meta_path = [finder for finder in sys.meta_path if not getattr(finder,'_population_source_finder',False)]
sys.meta_path.insert(0, _PopulationSourceFinder())
del _prior_controller, _prior_source_binding, _frame_code, _bootstrap_bytes
from population_runtime.adapter import AzerothCoreAdapter
from population_runtime.controller import PopulationController
from population_runtime.state import _validate_parent
from quest_policy import CadencedPopulationStrategy
# The accepted seed branch is selected BEFORE any GearInputs/extension/graph loading.
_acquisition_mode = _source_os.environ.get('PBGUIDE_ACQUISITION_MODE','gear')
if _acquisition_mode == 'native-seed-v1':
    from native_seed_policy import NativeSeedPolicy
    from capture_native_acquisition import catalogue as _validate_seed_catalogue, decode as _decode_seed
    _seed_path = Path(_source_os.environ['PBGUIDE_NATIVE_CATALOGUE_PATH'])
    _seed_raw = _source_bytes(_seed_path)
    _seed_sha = _source_hashlib.sha256(_seed_raw).hexdigest()
    if _seed_sha != _source_os.environ['PBGUIDE_NATIVE_CATALOGUE_SHA256']:
        raise ValueError('native seed original catalogue custody')
    _seed_catalogue = _decode_seed(_seed_raw)
    _validate_seed_catalogue(_seed_catalogue)
    _strategy_type = NativeSeedPolicy
    _strategy_arguments = dict(prepared_catalogue=_seed_catalogue, source_sha256=_seed_sha)
elif _acquisition_mode == 'gear':
    from gear_policy import GearInputs, GearPolicy
    _gear_inputs = GearInputs.from_binding(_source_os.environ['PBGUIDE_GEAR_BINDING_PATH'],
                                          _source_os.environ['PBGUIDE_GEAR_BINDING_SHA256'])
    _strategy_type = GearPolicy
    _strategy_arguments = dict(inputs=_gear_inputs, source_sha256=_gear_inputs.binding_sha256)
else:
    raise ValueError('unknown acquisition mode')
for port in ("strategic_read_quest_snapshot", "strategic_set_quest_goal", "strategic_renew_quest_goals"):
    if not callable(getattr(azerothcore, port, None)):
        raise RuntimeError("required strategic native port unavailable: " + port)
from policy_continuity import stage as _stage_policy, source_contract as _source_contract
if _population_source_binding != _source_contract(library, Path(__file__)):
    raise ValueError("population source changed during import")
_prior_namespace = globals().get('__previous_population_namespace__') or {}
_prior_strategy = _prior_namespace.get('strategy')
if _prior_namespace.get('_native_custody_ack_queue') is not None:
    raise RuntimeError('native custody queue must be settled before policy reload')
_renewal_observer_armed = True  # Completed acceptance observer stays inert across fresh starts.
controller, __commit_reload__, _continuous_policy, _prior_next_tick, _config_sha = _stage_policy(
    root.parent / 'policy', globals().pop('__previous_population_namespace__', None),
    PopulationController, AzerothCoreAdapter(azerothcore), lambda: int(time.time()),
    source_binding=_population_source_binding, source_paths=(library, Path(__file__)))
# Hash immutable policy inputs once, outside the timed world callback.
import hashlib as _policy_hashlib
import json as _policy_json
_policy_marker = {
    'config_sha256': _config_sha,
    'schedule_sha256': _policy_hashlib.sha256(controller.config.newcomer_schedule_path.read_bytes()).hexdigest()
        if controller.config.newcomer_schedule_path is not None else None,
    'rank_denominator': controller.config.rank_denominator,
    'admission_capacity': controller.config.admission_capacity,
    'action_batch_size': controller.config.action_batch_size,
    'bands': [{'name': b.name, 'share': str(b.share), 'ceiling_mode': b.ceiling_mode,
               'owner_level_offset': b.owner_level_offset, 'minimum_slots': b.minimum_slots}
              for b in controller.config.bands],
    'manifest_digest': controller.manifest.manifest_digest,
    'policy_continuity': _continuous_policy,
}
_policy_marker_pending = True
_capacity_read = controller.adapter.population_read_capacity_snapshot
_capacity_last = None
def _capacity_observed():
    global _capacity_last
    result = _capacity_read()
    signature = (result.valid, result.online_limit)
    if signature != _capacity_last:
        print('CPI202_NATIVE_CAPACITY ' + _policy_json.dumps({
            'valid': result.valid, 'generation': result.generation,
            'online_limit': result.online_limit, 'observed_epoch': result.observed_epoch,
            'config_sha256': _policy_marker['config_sha256'],
            'revision': controller.state.revision,
            'manifest_digest': controller.manifest.manifest_digest,
        }, sort_keys=True), flush=True)
        _capacity_last = signature
    return result
controller.adapter.population_read_capacity_snapshot = _capacity_observed
# Both layers use the same fenced
# world-thread tick. The strategic library does not own population scheduling.
# Observability events commit together before this strategy tick returns/raises.
# Native effects, QuestMemory and population state keep their existing durability.
_strategic_audit_rows = []

def strategic_audit(record):
    row = dict(record, epoch=int(time.time()), revision=controller.state.revision)
    _strategic_audit_rows.append(_policy_json.dumps(row, sort_keys=True,
        separators=(',', ':')).encode('ascii') + b'\n')

def _flush_strategic_audit():
    if not _strategic_audit_rows:
        return
    import os
    path = controller.config.audit_path.with_name('strategic-audit.jsonl')
    _validate_parent(path)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(b''.join(_strategic_audit_rows))
        stream.flush()
        os.fsync(stream.fileno())

def _strategy_tick_batched(bindings):
    original_error = None
    try:
        return _strategy_tick_unbatched(bindings)
    except BaseException as error:
        original_error = error
        raise
    finally:
        try:
            _flush_strategic_audit()
        except BaseException as flush_error:
            # Never retain a successful-renewal audit cache after failed commit.
            strategy._renewal_audits.clear()
            strategy._gear_audits.clear()
            if original_error is not None:
                raise RuntimeError('strategic audit flush failed: '
                    + type(flush_error).__name__ + ': ' + str(flush_error)
                    + '; original strategy error: ' + type(original_error).__name__
                    + ': ' + str(original_error)) from original_error
            raise
        finally:
            _strategic_audit_rows.clear()
# Installed binary identity from the verified TWELVE package receipt; no per-tick hash.
strategy = _strategy_type(azerothcore, strategic_audit, **_strategy_arguments,
    max_per_tick=controller.config.quest_discovery_budget,
    renewal_budget=controller.config.quest_renewal_budget,
    tick_interval_seconds=controller.config.quest_tick_interval_seconds,
    memory_path=controller.config.audit_path.with_name('strategic-state.json'))
strategy._acquisition_mode = _acquisition_mode
if _continuous_policy and _prior_strategy is not None:
    if getattr(_prior_strategy, '_acquisition_mode', None) != _acquisition_mode:
        raise RuntimeError('acquisition mode changed; refuse cross-mode schedule inheritance')
    if getattr(_prior_strategy, 'acquisition_capture', None) is not None:
        # Preserve the prior interpreter and original receipt lifetime on rejected reload.
        raise RuntimeError('finite capture must be drained and explicitly released before policy reload')
    strategy.inherit_schedule(_prior_strategy)
del _prior_namespace, _prior_strategy
_strategy_tick_unbatched = strategy.tick
strategy.tick = _strategy_tick_batched
cadenced = CadencedPopulationStrategy(controller, strategy)
if _continuous_policy:
    cadenced._next_tick = _prior_next_tick
print('CPI211_GEAR_POLICY_SOURCE_CANDIDATE_LOADED', len(controller.manifest.identities), flush=True)

_capture_tick_active = False

def _capture_in_tick():
    return _capture_tick_active


def arm_acquisition_capture(context, identity_key, binding, slot_key, source_id, snapshot_id, source_revision):
    """OFF TICK opt-in via existing authenticated execution seam; no native reads or implicit policy."""
    if _capture_tick_active:
        raise RuntimeError('capture arm must be off tick')
    if strategy.acquisition_capture is not None:
        raise RuntimeError('finite capture already armed; drain and explicitly release it first')
    # This import is opt-in and the source loader binds both minimal runtime modules.
    from capture_native_acquisition import FiniteAcquisitionCapture
    strategy.acquisition_capture = FiniteAcquisitionCapture(azerothcore, context, identity_key,
        binding, slot_key, source_id, snapshot_id, source_revision,
        policy_generation=strategy.source_sha256, in_tick=_capture_in_tick)
    return dict(armed=True, ready=False)

_native_custody_ack_queue = None

def prepare_native_custody_ack(original_descriptor, original_root, queue_path):
    """OFF TICK durable original verification and queue publication; never native release here."""
    global _native_custody_ack_queue
    if _capture_tick_active or _native_custody_ack_queue is not None:
        raise RuntimeError('native custody prepare requires off tick and no retained queue')
    from native_custody_ack import NativeCustodyAckQueue
    _native_custody_ack_queue = NativeCustodyAckQueue.prepare(original_descriptor,original_root,queue_path,in_tick=_capture_in_tick)
    return dict(queued=len(_native_custody_ack_queue.pending()),native_release=False)

def recover_native_custody_ack(queue_path):
    """OFF TICK explicit same-owner closed-record recovery, including prior sessions/generations."""
    global _native_custody_ack_queue
    if _capture_tick_active or _native_custody_ack_queue is not None:
        raise RuntimeError('native custody recovery requires off tick and no retained queue')
    from native_custody_ack import NativeCustodyAckQueue
    _native_custody_ack_queue = NativeCustodyAckQueue.recover(queue_path,in_tick=_capture_in_tick)
    return dict(queued=len(_native_custody_ack_queue.pending()),native_release=False)

def settle_native_custody_ack():
    """OFF TICK: persist only validated native results; release in-memory queue after settlement."""
    global _native_custody_ack_queue
    if _capture_tick_active or _native_custody_ack_queue is None:
        raise RuntimeError('native custody settlement requires off tick and retained queue')
    result=_native_custody_ack_queue.settle()
    if not _native_custody_ack_queue.pending():_native_custody_ack_queue=None
    return result

def arm_native_quest_capture(identity_key, binding, exact_slot_key, snapshot_id, exporter_revision):
    """OFF TICK: opt in to actual native event receipts, before any accepted dataset exists."""
    if _capture_tick_active or _acquisition_mode != 'native-seed-v1':
        raise RuntimeError('native seed receipt arm requires off-tick native seed mode')
    if strategy.acquisition_capture is not None:
        raise RuntimeError('native receipt retained; drain and explicitly release first')
    from native_seed_policy import NativeQuestReceiptCapture
    from capture_native_acquisition import prepare_current_observation
    slots = _validate_seed_catalogue(_seed_catalogue)
    selected = [slot for slot in slots if slot['key'] == exact_slot_key]
    if len(selected) != 1:
        raise ValueError('native seed exact original slot missing')
    context = prepare_current_observation(_seed_catalogue, selected[0]['source_entry'])
    strategy.acquisition_capture = NativeQuestReceiptCapture(azerothcore,context,identity_key,binding,
        selected[0],snapshot_id,exporter_revision,in_tick=_capture_in_tick,policy_generation=strategy.source_sha256)
    return dict(armed=True,ready=False)

def recover_native_quest_capture(original_descriptor,original_root):
    """OFF TICK: fetch late finalized original records on later admitted ticks, without a new quest goal."""
    if _capture_tick_active or strategy.acquisition_capture is not None:
        raise RuntimeError('native receipt recovery requires off tick and no retained capture')
    from native_seed_policy import NativeQuestReceiptCapture
    strategy.acquisition_capture=NativeQuestReceiptCapture.recover(azerothcore,original_descriptor,original_root,
        in_tick=_capture_in_tick,policy_generation=strategy.source_sha256)
    return dict(recovering=True,ready=False)

def drain_acquisition_capture(output):
    """OFF TICK original receipt retention. Normalization/producer work stays outside this callback."""
    if _capture_tick_active:
        raise RuntimeError('capture drain must be off tick')
    if strategy.acquisition_capture is None:
        raise RuntimeError('no finite capture armed')
    return strategy.acquisition_capture.drain(output)

def release_acquisition_capture():
    if _capture_tick_active:
        raise RuntimeError('capture release must be off tick')
    observer = strategy.acquisition_capture
    release = getattr(observer, 'request_release', None)
    if callable(release):
        result = release()
        if result['released']:strategy.acquisition_capture = None
        return result
    strategy.acquisition_capture = None

def population_tick(diff_ms):
    global _policy_marker_pending, _capture_tick_active
    if _capture_tick_active:
        raise RuntimeError('recursive population callback')
    _capture_tick_active = True
    try:
        before_revision = controller.state.revision
        accepted = cadenced.tick(diff_ms)
        if accepted and _native_custody_ack_queue is not None:
            try:
                _native_custody_ack_queue.apply(azerothcore)
            except Exception as error:
                # Preserve queue/custody for explicit off-tick recovery; ordinary goal results stay intact.
                _native_custody_ack_queue.runtime_results=dict(error=type(error).__name__+': '+str(error))
                _native_custody_ack_queue.document['last_send_error']=type(error).__name__+': '+str(error)
        observer = strategy.acquisition_capture
        if observer is not None:
            if accepted:
                try:
                    observer.tick(strategy.source_sha256)
                except Exception as error:
                    observer.fail(type(error).__name__ + ': ' + str(error))
            else:
                observer.fail('population admission/publication callback refused')
        if accepted and _policy_marker_pending and controller.state.revision > before_revision:
            print('CPI194_POLICY_TICK_OK ' + _policy_json.dumps(
                dict(_policy_marker, revision=controller.state.revision), sort_keys=True), flush=True)
            _policy_marker_pending = False
        return accepted
    except Exception as error:
        if strategy.acquisition_capture is not None:
            strategy.acquisition_capture.fail('population callback failed: ' + type(error).__name__ + ': ' + str(error))
        raise
    finally:
        _capture_tick_active = False

# Opt-in bootstrap-only diagnostic. No imported runtime module is mutated.
import functools as _timing_functools
import types as _timing_types

class CallbackTiming:
    def __init__(self, clock=time.perf_counter_ns, emit=print):
        self.clock, self.emit = clock, emit
        self.stack, self.rows, self.active = [], {}, False
        self.samples = 0
        self.last_emit_ms = 0.0

    def call(self, label, function, *args, **kwargs):
        if not self.active:
            return function(*args, **kwargs)
        started = self.clock()
        frame = [0]
        self.stack.append(frame)
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = self.clock() - started
            self.stack.pop()
            if self.stack:
                self.stack[-1][0] += elapsed
            row = self.rows.setdefault(label, [0, 0])
            row[0] += 1
            row[1] += elapsed - frame[0]

    def wrap(self, label, function):
        @_timing_functools.wraps(function)
        def measured(*args, **kwargs):
            return self.call(label, function, *args, **kwargs)
        return measured

    def run(self, callback, revision, diff_ms):
        self.rows = {}
        before = revision()
        started = self.clock()
        self.active = True
        error = None
        try:
            return self.call('callback.other', callback, diff_ms)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.active = False
            elapsed = (self.clock() - started) / 1e6
            advanced = revision() != before
            if advanced:
                self.samples += 1
            if elapsed >= 60 or error or (advanced and (self.samples == 1 or self.samples % 30 == 0)):
                event = {'revision': revision(), 'python_ms': round(elapsed, 3),
                         'previous_emit_ms': round(self.last_emit_ms, 3), 'error': error,
                         'self_ms': {k: [v[0], round(v[1] / 1e6, 3)] for k, v in sorted(self.rows.items())}}
                output_started = self.clock()
                try:
                    self.emit('CPI223_CALLBACK_TIMING ' + _policy_json.dumps(event, sort_keys=True), flush=True)
                except Exception:
                    # Diagnostics must not replace a callback exception/return value.
                    pass
                self.last_emit_ms = (self.clock() - output_started) / 1e6

_timing = CallbackTiming()
# Clone function globals for this controller instance so save/audit measurement
# does not alter the imported module, other callbacks or reload rollback state.
for _name in ('_tick', '_fail_closed', '_admit_due_batch', '_bind_and_admit',
              '_refresh_progress', '_artifacts_unchanged', '_assignments', '_desired_sessions'):
    _bound = getattr(controller, _name)
    _function = _bound.__func__
    _globals = dict(_function.__globals__)
    for _io in ('save_state', 'append_audit'):
        if _io in _globals:
            _globals[_io] = _timing.wrap(_io, _globals[_io])
    _clone = _timing_types.FunctionType(_function.__code__, _globals,
                                       _function.__name__, _function.__defaults__, _function.__closure__)
    _clone.__kwdefaults__ = _function.__kwdefaults__
    setattr(controller, _name, _timing.wrap('controller.' + _name,
                                           _timing_types.MethodType(_clone, controller)))
_original_adapter_call = controller.adapter._call

def _measured_adapter_call(name, *args):
    return _timing.call('native.' + name, _original_adapter_call, name, *args)
controller.adapter._call = _measured_adapter_call

class _TimedStrategicNative:
    def __init__(self, native):
        self.native = native
    def __getattr__(self, name):
        return _timing.wrap('native.' + name, getattr(self.native, name))
strategy.native = _TimedStrategicNative(strategy.native)
strategy.tick = _timing.wrap('strategy.other', strategy.tick)
# This alias belongs only to the candidate bootstrap namespace.
_flush_strategic_audit = _timing.wrap('strategic.append_audit', _flush_strategic_audit)
_untimed_population_tick = population_tick

def population_tick(diff_ms):
    return _timing.run(_untimed_population_tick, lambda: controller.state.revision, diff_ms)

# Finite acceptance observation; no extra native calls or repeated hot-reload arming.
"""Finite observation of existing renewal replies; never initiates native work."""
import json
import time


def observe_renewals(original, emit=print, clock=time.monotonic):
    deadline = None
    pair = None
    first = None
    remaining = 16
    stopped = False

    def wrapped(rows):
        nonlocal pair, first, remaining, stopped, deadline
        # Keep native exceptions and the exact returned object unchanged.
        if stopped:
            return original(rows)
        try:
            started = clock()
            if deadline is None:
                deadline = started + 120.0
        except Exception:
            stopped = True
            return original(rows)
        result = original(rows)
        try:
            ended = clock()
            if ended >= deadline:
                stopped = True
                return result
            for row, reply in zip(rows, result, strict=True):
                candidate = (row['identity_key'], row['quest_id'])
                if pair is None:
                    if reply['status'] != 'accepted' or reply['reason'] != 'renewed':
                        continue
                    pair, first = candidate, ended
                if candidate != pair:
                    continue
                remaining -= 1
                terminal = ended - first >= 12.0 or remaining == 0 or reply['status'] != 'accepted'
                stopped = terminal
                emit('CPI207_RENEWAL_OBSERVATION ' + json.dumps({
                    'identity_key': pair[0], 'quest_id': pair[1],
                    'monotonic': ended, 'epoch': time.time(), 'elapsed_seconds': ended - first,
                    'batch_size': len(rows), 'native_ms': (ended - started) * 1000,
                    'status': reply['status'], 'reason': reply['reason'],
                    'remaining': remaining, 'terminal': terminal,
                }, sort_keys=True), flush=True)
                break
        except Exception:
            stopped = True
        return result

    return wrapped

if not _renewal_observer_armed:
    strategy.native.strategic_renew_quest_goals = observe_renewals(strategy.native.strategic_renew_quest_goals)
_renewal_observer_armed = True

def _observe_earned_coverage(original, admitted, marker, clock, emit):
    # Diagnostic only: a durable one-shot marker prevents fresh-start rearming.
    expected = frozenset(admitted)
    seen, special = set(), {}
    deadline = None
    inert = len(expected) != 2500 or marker.exists()
    milestone = 0
    def observed(*args, **kwargs):
        nonlocal deadline, inert, milestone
        if not inert and deadline is None:
            try:
                with marker.open('x') as stream:
                    stream.write('one-shot coverage observation claimed\n')
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
                directory = os.open(marker.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                deadline = clock() + 900
            except BaseException:
                inert = True
        result = original(*args, **kwargs)
        if inert:
            return result
        try:
            now = clock()
            if now >= deadline:
                inert = True
                emit({'event': 'deadline', 'covered': len(seen), 'expected': len(expected), 'special': special})
                return result
            for row in result:
                if row.identity_key in expected:
                    seen.add(row.identity_key)
                    if row.character_guid in (330, 1640):
                        special[str(row.character_guid)] = {'map': row.map_id, 'zone': row.zone_id}
            complete = len(seen) == len(expected)
            if complete or len(seen) // 2500 > milestone:
                milestone = len(seen) // 2500
                inert = complete
                emit({'event': 'complete' if complete else 'coverage', 'covered': len(seen),
                      'expected': len(expected), 'special': special})
        except BaseException:
            inert = True
        return result
    return observed

controller.adapter.population_read_earned_snapshot = _observe_earned_coverage(
    controller.adapter.population_read_earned_snapshot, controller.state.admitted_identity_keys,
    controller.config.audit_path.with_name('namegen-2500-earned-coverage.once'), time.monotonic,
    lambda row: print('CPI194_EARNED_COVERAGE ' + _policy_json.dumps(dict(row,
        epoch=int(time.time()), revision=controller.state.revision), sort_keys=True), flush=True))
