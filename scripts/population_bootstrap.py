"""2,500-row target with explicit admission holds; fresh identities admit at level one."""
import sys
import time
from pathlib import Path
import azerothcore
root = Path(str(globals()['__script_root__'])).resolve(strict=True).parent
library = root / 'lib'
if str(library) not in sys.path:
    sys.path.insert(0, str(library))
# PythonEngine stages a fresh script namespace but keeps interpreter alive.
# Drop only modules changed by this rollout; shared value classes must remain
# identical so a Python-only reload can preserve active incumbents.
for _module_name in tuple(sys.modules):
    if _module_name in {
        'population_runtime.config',
        'population_runtime.controller',
        'policy_continuity',
    }:
        del sys.modules[_module_name]
from population_runtime.adapter import AzerothCoreAdapter
from population_runtime.controller import PopulationController
from population_runtime.state import _validate_parent
from quest_policy import QuestPolicy, CadencedPopulationStrategy
for port in ("strategic_read_quest_snapshot", "strategic_set_quest_goal", "strategic_renew_quest_goals"):
    if not callable(getattr(azerothcore, port, None)):
        raise RuntimeError("required strategic native port unavailable: " + port)
from policy_continuity import stage as _stage_policy
_prior_namespace = globals().get('__previous_population_namespace__') or {}
_prior_strategy = _prior_namespace.get('strategy')
_renewal_observer_armed = True  # Completed acceptance observer stays inert across fresh starts.
controller, __commit_reload__, _continuous_policy, _prior_next_tick, _config_sha = _stage_policy(
    root.parent / 'policy', globals().pop('__previous_population_namespace__', None),
    PopulationController, AzerothCoreAdapter(azerothcore), lambda: int(time.time()))
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
            if original_error is not None:
                raise RuntimeError('strategic audit flush failed: '
                    + type(flush_error).__name__ + ': ' + str(flush_error)
                    + '; original strategy error: ' + type(original_error).__name__
                    + ': ' + str(original_error)) from original_error
            raise
        finally:
            _strategic_audit_rows.clear()
# Installed binary identity from the verified TWELVE package receipt; no per-tick hash.
strategy = QuestPolicy(azerothcore, strategic_audit,
    max_per_tick=controller.config.quest_discovery_budget,
    renewal_budget=controller.config.quest_renewal_budget,
    tick_interval_seconds=controller.config.quest_tick_interval_seconds,
    memory_path=controller.config.audit_path.with_name('strategic-state.json'),
    source_sha256='29b534a805ab591c7388e0f64a6b0ca34a21709ffc05473fdf6206a621a93cb6')
if _continuous_policy and _prior_strategy is not None:
    strategy.inherit_schedule(_prior_strategy)
_strategy_tick_unbatched = strategy.tick
strategy.tick = _strategy_tick_batched
cadenced = CadencedPopulationStrategy(controller, strategy)
if _continuous_policy:
    cadenced._next_tick = _prior_next_tick
print('CPI207_QUEST_POLICY_LOADED', len(controller.manifest.identities), flush=True)

def population_tick(diff_ms):
    global _policy_marker_pending
    before_revision = controller.state.revision
    accepted = cadenced.tick(diff_ms)
    if accepted and _policy_marker_pending and controller.state.revision > before_revision:
        print('CPI194_POLICY_TICK_OK ' + _policy_json.dumps(
            dict(_policy_marker, revision=controller.state.revision), sort_keys=True), flush=True)
        _policy_marker_pending = False
    return accepted

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
