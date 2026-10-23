"""Population entry: explicit finite witness; no bulk population controller."""
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import stat
import sys

_previous = globals().get('__previous_population_namespace__') or {}
if _previous.get('_witness') is not None:
    raise RuntimeError('witness namespace retained; use finite commands, never reload over custody')
_library = Path(str(globals()['__script_root__'])).resolve(strict=True).parent / 'lib'

def _original(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('absolute nonsymlink physical input required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('regular physical input required')
        return stream.read()

def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise ValueError('duplicate physical config key')
        value[key] = item
    return value

_config_path = os.environ['PBGUIDE_WITNESS_CONFIG_PATH']
_config_sha = os.environ['PBGUIDE_WITNESS_CONFIG_SHA256']
_config_raw = _original(_config_path)
if hashlib.sha256(_config_raw).hexdigest() != _config_sha:
    raise ValueError('selected witness physical config hash mismatch')
_config = json.loads(_config_raw, object_pairs_hook=_pairs)
_names = {'quest_policy', 'gear_policy', 'native_seed_policy', 'native_custody_ack',
          'capture_native_acquisition', 'ingest_capture', 'witness_runtime',
          'native_catalogue_export'}
_dependencies = _config['dependencies']
if set(_dependencies) != {name + '.py' for name in _names}:
    raise ValueError('witness exact dependency source closure required')
_sources = {}
for _name in sorted(_names):
    _path = _library / (_name + '.py')
    _data = _original(_path)
    if hashlib.sha256(_data).hexdigest() != _dependencies[_path.name]:
        raise ValueError('witness dependency source hash mismatch: ' + _name)
    _sources[_name] = (_path, _data)
# Compile verified original bytes, bypass stale pyc and reject cached foreign libraries.
for _name in _names:
    if _name in sys.modules:
        _module = sys.modules[_name]
        if getattr(_module, '_witness_source_sha256', None) != _dependencies[_name + '.py'] or Path(_module.__file__) != _sources[_name][0]:
            raise RuntimeError('witness dependency already loaded without exact source proof: ' + _name)

class _VerifiedLoader:
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        path, data = _sources[module.__name__]
        module.__file__ = str(path)
        exec(compile(data, str(path), 'exec'), module.__dict__)
        module._witness_source_sha256 = _dependencies[path.name]

class _VerifiedFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in _sources:
            return importlib.machinery.ModuleSpec(fullname, _VerifiedLoader(), origin=str(_sources[fullname][0]))
        return None

_finder = _VerifiedFinder()
sys.meta_path.insert(0, _finder)
try:
    import azerothcore
    from witness_runtime import Witness, load_config
    _checked_config = load_config(_config_path, _config_sha)
    if _checked_config != _config:
        raise ValueError('witness configuration changed during loading')
    _witness = Witness(azerothcore, _checked_config,
        audit=lambda row: print('CPI225_WITNESS_AUDIT ' + json.dumps(row, sort_keys=True), flush=True))
finally:
    sys.meta_path.remove(_finder)

_catalogue_export = None
if _checked_config['native_catalogue_capture_path'] is not None:
    from native_catalogue_export import CatalogueExporter
    _entry_path = Path(str(globals()['__script_root__'])) / 'native_witness.py'
    _entry_bytes = _original(_entry_path)
    _catalogue_export = CatalogueExporter(_checked_config['native_catalogue_capture_path'],
        sys.modules['capture_native_acquisition'], str(_entry_path),
        hashlib.sha256(_entry_bytes).hexdigest())

def population_tick(diff_ms):
    if _catalogue_export is not None and not _catalogue_export.enqueue_from_tick(azerothcore):
        return False
    return _witness.tick(diff_ms)

def witness_status():
    _witness.off_tick()
    import copy
    result = copy.deepcopy(_witness.status)
    observer = _witness.strategy.acquisition_capture
    result['capture'] = None if observer is None else dict(state=observer.state, error=observer.error,
        record_id=observer.native_record_id, disable_pending=observer.disable_pending,
        owner_quiescent=observer.owner_quiescent, release_requested=observer.release_requested)
    result['custody_ack'] = None if _witness.ack is None else dict(
        original_descriptor=copy.deepcopy(_witness.ack.document['original_descriptor']),
        ack_pairs=[(row['ack']['record_id'], row['ack']['record_sha256'])
                   for row in _witness.ack.document['records']],
        records=[dict(record_id=row['ack']['record_id'], status=row['status'], reason=row['reason'])
                 for row in _witness.ack.document['records']], results=copy.deepcopy(_witness.ack.runtime_results))
    result['required_acks'] = sorted(_witness.required_acks)
    result['applied_acks'] = sorted(_witness.applied_acks)
    result['gear_observation_request'] = None if _witness.gear_observation_request is None else dict(
        slot_key=_witness.gear_observation_request['slot_key'], source_id=_witness.gear_observation_request['source_id'])
    result['gear_observation'] = copy.deepcopy(_witness.gear_observation)
    result['native_catalogue_export'] = None if _catalogue_export is None else _catalogue_export.status()
    return result

def witness_command(mode):
    return _witness.command(mode)

def request_witness_session():
    return _witness.request_session()

def select_native_seed_slot(exact_slot_key):
    return _witness.select_seed_slot(exact_slot_key)

def queue_native_gear_observation(exact_slot_key):
    return _witness.queue_gear_observation(exact_slot_key)

def stage_accepted_gear(path, sha256):
    return _witness.stage_gear(path, sha256)

def arm_native_quest_capture(identity_key, binding, exact_slot_key, snapshot_id, exporter_revision):
    return _witness.arm(identity_key, binding, exact_slot_key, snapshot_id, exporter_revision)

def recover_native_quest_capture(original_descriptor, original_root):
    return _witness.recover_capture(original_descriptor, original_root)

def drain_acquisition_capture(output):
    return _witness.drain(output)

def release_acquisition_capture():
    return _witness.release_capture()

def prepare_native_custody_ack(original_descriptor, original_root, queue_path):
    return _witness.prepare_ack(original_descriptor, original_root, queue_path)

def recover_native_custody_ack(queue_path):
    return _witness.recover_ack(queue_path)

def settle_native_custody_ack():
    return _witness.settle_ack()
