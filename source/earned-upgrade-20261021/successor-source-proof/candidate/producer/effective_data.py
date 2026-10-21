"""Accepted adapter custody/canonical utilities; no frozen-source pin or resource quotas.

Retained from continuation07 effective_data.py; semantic builders must remain separate.
"""
import hashlib
from collections import Counter
import json
from pathlib import Path
from ingest_capture import CaptureError, decode, _digest, _uint

def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as error:
        raise CaptureError('canonical JSON requires representable finite values') from error

def measurement(value):
    data = canonical(value)
    return dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

def read_descriptor(descriptor, root):
    if not isinstance(descriptor, dict):
        raise CaptureError('file descriptor')
    path = descriptor.get('path', descriptor.get('file'))
    if not isinstance(path, str):
        raise CaptureError('file path')
    path = Path(path)
    if not path.is_absolute():
        if path.name != str(path) or path.name in {'.', '..'}:
            raise CaptureError('capture basename')
        path = root / path
    if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents):
        raise CaptureError('capture must be regular nonsymlink file')
    data = path.read_bytes()
    if type(descriptor.get('bytes')) is not int or descriptor['bytes'] < 0 or len(data) != descriptor['bytes'] or hashlib.sha256(data).hexdigest() != _digest(descriptor.get('sha256')):
        raise CaptureError('capture file custody')
    return data

def jsonl(data, descriptor):
    rows = [decode(line) for line in data.splitlines() if line]
    if type(descriptor.get('rows')) is not int or len(rows) != descriptor['rows']:
        raise CaptureError('capture row count')
    columns = descriptor.get('columns')
    if not isinstance(columns, list) or len(columns) != len(set(columns)) or any(not isinstance(c, str) for c in columns):
        raise CaptureError('capture columns')
    if any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
        raise CaptureError('capture row schema')
    return rows


def digest(value):
    return measurement(value)['sha256']

REPO = Path(__file__).resolve().parent
SCHEMAS = REPO / 'schemas'
FAMILIES = {
    'quest_reward': ('quest', 'quest_reward'), 'creature_loot': ('creature', 'loot_source'),
    'gameobject_loot': ('gameobject', 'loot_source'), 'vendor': ('vendor', 'vendor_source'),
    'trainer_recipe': ('trainer', 'trainer_spell'), 'craft': ('recipe', 'recipe_product'),
    'pvp_reward': ('vendor', 'vendor_source'), 'dungeon_quest': ('quest', 'quest_reward'),
    'dungeon_drop': ('creature', 'loot_source'), 'disenchant': ('item', 'loot_source'),
    'prospecting': ('item', 'loot_source'), 'milling': ('item', 'loot_source'),
    'fishing': ('loot_template', 'loot_source'), 'skinning': ('creature', 'loot_source')}
FACT_FIELDS = {'key', 'family', 'source_entry', 'item_entry', 'minimum_count', 'maximum_count',
    'probability', 'frontier', 'stages', 'phase_mask', 'spawn_mask', 'event_ids', 'pool_ids',
    'condition', 'group_size', 'copper', 'travel', 'attempt', 'risk', 'contention', 'capabilities',
    'profession', 'alternative', 'reagents', 'raw_refs', 'variant', 'unresolved'}

def validate_documents(documents):
    # Use the shipped schemas, including their real representational limits.
    import jsonschema
    from referencing import Registry, Resource
    schemas = {p.name: json.loads(p.read_text()) for p in SCHEMAS.glob('*.schema.json')}
    registry = Registry().with_resources((s['$id'], Resource.from_contents(s)) for s in schemas.values())
    def check(value, name, role):
        validator = jsonschema.Draft202012Validator(schemas[name], registry=registry, format_checker=jsonschema.FormatChecker())
        error = next(validator.iter_errors(value), None)
        if error:
            location = '.'.join(map(str, error.absolute_path))
            actual = len(error.instance) if isinstance(error.instance, (dict,list,str)) else error.instance
            raise CaptureError(f'{role}.{location}: schema {error.validator} actual={actual} required={error.validator_value}')
    for role, name in (('graph', 'content-graph-v1.schema.json'), ('manifest', 'content-manifest-v1.schema.json')):
        check(documents[role], name, role)
    if not isinstance(documents['projection'], list):
        raise CaptureError('projection must be array')
    for index, item in enumerate(documents['projection']):
        check(item, 'item-source-v1.schema.json', f'projection[{index}]')

def compiler_identity():
    """Bind the bytes actually used; a caller revision label is insufficient."""
    paths = [Path(__file__).with_name(n) for n in ('ingest_capture.py','dbc_overlay.py','effective_data.py','generation.py','verify_generation.py')]
    paths += [REPO / 'compile_content_graph.py']
    paths += [SCHEMAS / n for n in ('content-graph-v1.schema.json','content-manifest-v1.schema.json','item-source-v1.schema.json','common-v1.schema.json')]
    paths += [REPO / 'EIGHT-STORE-MAP.json']
    identities={str(p.relative_to(REPO)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for name in ('first_current_input.py','capture_manifest.py','capture_native_acquisition.py','normalize_current_quest.py',
                 'ordinary_kill_proof.py','ordinary-kill-census-pin.json'):
        identities['caller/'+name]=hashlib.sha256((REPO.parent/name).read_bytes()).hexdigest()
    return digest(identities)

def load_inputs(path, expected):
    """Only custody/shape is shared with the independent semantic verifier."""
    path = Path(path)
    raw = read_descriptor({'path': str(path.absolute()), 'bytes': path.stat().st_size, 'sha256': expected}, path.parent)
    m = decode(raw)
    if not isinstance(m, dict) or set(m) != {'schema', 'binding', 'tables', 'native', 'policy', 'config', 'migrations', 'dbc_files'} or m['schema'] != 'core1-sealed-acquisition-v1':
        raise CaptureError('generation manifest shape')
    binding = m['binding']
    fields = {'candidate_id', 'core_revision', 'playerbots_revision', 'progression_revision', 'content_revision', 'compiler_revision', 'captured_at', 'snapshot_id', 'config_digest', 'policy_digest', 'world_dump_digest', 'dbc_set_digest', 'migration_digest'}
    if not isinstance(binding, dict) or set(binding) != fields or not isinstance(binding['snapshot_id'], str) or not binding['snapshot_id']:
        raise CaptureError('generation binding shape')
    if binding['compiler_revision'] != compiler_identity():
        raise CaptureError('actual producer/compiler/schema bytes mismatch')
    generation = digest(binding)
    def read(desc):
        if not isinstance(desc, dict) or desc.get('generation') != binding['snapshot_id']:
            raise CaptureError('mixed descriptor generation')
        return read_descriptor(desc, path.parent)
    if hashlib.sha256(read(m['config'])).hexdigest() != _digest(binding['config_digest']):
        raise CaptureError('configuration binding')
    if digest(m['migrations']) != _digest(binding['migration_digest']):
        raise CaptureError('ordered SQL binding')
    names = set()
    for ordinal, d in enumerate(m['migrations']):
        if d.get('ordinal') != ordinal or d.get('name') in names:
            raise CaptureError('ordered SQL ordinal/name')
        names.add(d['name']); read(d)
    if digest(m['dbc_files']) != _digest(binding['dbc_set_digest']):
        raise CaptureError('DBC set binding')
    names = set()
    for d in m['dbc_files']:
        if d.get('name') in names:
            raise CaptureError('duplicate DBC name')
        names.add(d.get('name')); read(d)
    tables = {}
    if not isinstance(m['tables'], dict):
        raise CaptureError('table descriptors')
    for table, desc in m['tables'].items():
        if not isinstance(table, str) or not table.replace('_', '').isalnum():
            raise CaptureError('table name')
        tables[table] = jsonl(read(desc), desc)
    if digest(m['tables']) != _digest(binding['world_dump_digest']):
        raise CaptureError('world descriptor binding')
    policy_bytes = read(m['policy'])
    if hashlib.sha256(policy_bytes).hexdigest() != _digest(binding['policy_digest']):
        raise CaptureError('phase policy binding')
    policy = decode(policy_bytes)
    if not isinstance(policy, dict) or set(policy) != {'generation_snapshot', 'stages'} or policy['generation_snapshot'] != binding['snapshot_id']:
        raise CaptureError('phase generation')
    stages = policy['stages']
    if not isinstance(stages, list) or [s.get('id') for s in stages] != [f'S{i:02}' for i in range(1, 38)]:
        raise CaptureError('full37 stage identities/order required')
    for stage in stages:
        if set(stage) != {'id', 'name', 'loaded', 'input_complete', 'reason'} or type(stage['loaded']) is not bool or type(stage['input_complete']) is not bool or not isinstance(stage['name'], str) or not stage['name'] or not isinstance(stage['reason'], str):
            raise CaptureError('stage policy shape')
        if not stage['input_complete'] and not stage['reason'] or stage['loaded'] and not stage['input_complete']:
            raise CaptureError('missing/loaded stage input reason')
    native = decode(read(m['native']))
    if not isinstance(native, dict) or set(native) != {'schema', 'generation', 'exporter_revision', 'raw_digest', 'loaded_generation_verified', 'items', 'acquisitions', 'dispositions', 'family_dispositions', 'unresolved', 'slot_custody'} or native['schema'] != 'core1-native-acquisition-facts-v1':
        raise CaptureError('native export shape')
    if native['generation'] != generation or native['raw_digest'] != binding['world_dump_digest']:
        raise CaptureError('mixed native/raw generation')
    if type(native['loaded_generation_verified']) is not bool or not isinstance(native['exporter_revision'], str) or not native['exporter_revision']:
        raise CaptureError('native parity evidence')
    custody = native['slot_custody']
    if not isinstance(custody, dict) or set(custody) != {'catalogue', 'observation_generation', 'slots', 'source_revision', 'snapshot_id'} or custody['snapshot_id'] != binding['snapshot_id'] or custody['source_revision'] != native['exporter_revision']:
        raise CaptureError('native slot custody shape/generation')
    catalog = decode(read(custody['catalogue']))
    # Independently reconstruct original identities/counts; do not call the capture converter.
    slots = []
    for quest in catalog['quests']:
        for reward in quest['rewards']:
            slots.append(dict(key=f"quest:{quest['entry']}:{reward['kind']}:{reward['index']}",source_entry=quest['entry'],
                item_entry=reward['item'],minimum_count=reward['count'],maximum_count=reward['count'],native_reward=reward,native_quest=quest))
    if canonical(slots) != canonical(custody['slots']) or catalog['observation_generation'] != custody['observation_generation'] or catalog['item_count'] != len(catalog['items']) or catalog['quest_count'] != len(catalog['quests']) or catalog['reward_count'] != len(slots):
        raise CaptureError('native slot custody original census/counts')
    original_items = {row['entry']:{key:value for key,value in row.items() if key!='entry'} for row in catalog['items']}
    if len(original_items) != len(catalog['items']) or {row['entry']:row['attributes'] for row in native['items']} != original_items or len(native['items']) != len(original_items):
        raise CaptureError('native original item census/attributes')
    represented = {fact['key']: fact for fact in native['acquisitions']}
    deferred = {record['source']: record for record in native['unresolved'] if isinstance(record, dict) and record.get('reason') == 'nonexecutable_native_slot'}
    if len(represented) != len(native['acquisitions']) or set(represented) & set(deferred) or set(represented) | set(deferred) != {slot['key'] for slot in slots}:
        raise CaptureError('native slot executable/unresolved coverage')
    for slot in slots:
        if slot['key'] in represented:
            fact = represented[slot['key']]
            if any(fact[field] != slot[field] for field in ('key','source_entry','item_entry','minimum_count','maximum_count')):
                raise CaptureError('native executable slot identity/yield')
        elif deferred[slot['key']]['native_slot'] != slot:
            raise CaptureError('native unresolved original slot')
    refs = {(t, i, digest(row)) for t, rows in tables.items() for i, row in enumerate(rows)}
    accounting = Counter()
    for field in ('items','acquisitions','dispositions','unresolved'):
        if not isinstance(native[field], list):
            raise CaptureError('native export array: '+field)
    for fact in native['acquisitions']:
        if not isinstance(fact, dict) or set(fact) != FACT_FIELDS:
            raise CaptureError('native acquisition fields')
        if not isinstance(fact['raw_refs'], list) or not fact['raw_refs']:
            raise CaptureError('native source requires raw provenance')
        if len({canonical(r) for r in fact['raw_refs']}) != len(fact['raw_refs']):
            raise CaptureError('duplicate native raw reference')
        if not isinstance(fact['unresolved'], list) or not isinstance(fact['variant'], dict):
            raise CaptureError('native unresolved/variant shape')
        for ref in fact['raw_refs']:
            if not isinstance(ref, list) or len(ref) != 3 or type(ref[1]) is not int or tuple(ref) not in refs:
                raise CaptureError('native source raw reference')
            accounting[tuple(ref)] += 1
    for d in native['dispositions']:
        if not isinstance(d, dict) or set(d) != {'raw_ref', 'reason'} or not isinstance(d['reason'], str) or not d['reason'] or tuple(d['raw_ref']) not in refs:
            raise CaptureError('row omission disposition')
        accounting[tuple(d['raw_ref'])] += 1
    if set(accounting) != refs:
        raise CaptureError('unaccounted raw rows')
    disposition_refs = [tuple(d['raw_ref']) for d in native['dispositions']]
    if len(set(disposition_refs)) != len(disposition_refs) or any(accounting[r] != 1 for r in disposition_refs):
        raise CaptureError('ambiguous omitted row')
    families = native['family_dispositions']
    if not isinstance(families, dict) or (set(FAMILIES) | {f['family'] for f in native['acquisitions']}) - set(families) or any(not isinstance(v, str) or not v for v in families.values()):
        raise CaptureError('all supported/unsupported family dispositions required')
    return m, tables, policy, native
