#!/usr/bin/env python3
"""Seal original loaded catalogue plus separately evidenced semantics for the accepted producer."""
import argparse
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from capture_native_acquisition import (CaptureError, canonical, digest, read_descriptor, decode,
    catalogue, REQUIRED_SEMANTICS, FAMILIES, FACT_FIELDS, accepted_offline_modules)
accepted_offline_modules()
from effective_data import compiler_identity, load_inputs, jsonl
import generation as producer

BINDING_FIELDS = {'candidate_id', 'core_revision', 'playerbots_revision', 'progression_revision',
    'content_revision', 'compiler_revision', 'captured_at', 'snapshot_id', 'config_digest',
    'policy_digest', 'world_dump_digest', 'dbc_set_digest', 'migration_digest'}
INPUT_FIELDS = {'binding', 'tables', 'policy', 'config', 'migrations', 'dbc_files'}


def require(condition, reason):
    if not condition:
        raise CaptureError(reason)


def prepare(path, expected):
    path = Path(path).absolute()
    raw = read_descriptor(dict(path=str(path), bytes=path.stat().st_size, sha256=expected), path.parent)
    request = decode(raw)
    require(isinstance(request, dict) and set(request) == INPUT_FIELDS | {
        'schema', 'catalog', 'semantics', 'attestation', 'source_files', 'locale_files', 'evidence'},
        'capture request shape')
    require(request['schema'] == 'core1-native-capture-request-v1', 'capture request schema')
    binding = request['binding']
    require(isinstance(binding, dict) and set(binding) == BINDING_FIELDS, 'capture binding fields')
    require(isinstance(binding['snapshot_id'], str) and bool(binding['snapshot_id']), 'capture snapshot')
    require(binding['compiler_revision'] == compiler_identity(), 'actual accepted compiler identity')
    generation = digest(binding)
    descriptors = []

    def read(d):
        require(isinstance(d, dict) and d.get('generation') == binding['snapshot_id'], 'mixed capture descriptor generation')
        data = read_descriptor(d, path.parent)
        descriptors.append(copy.deepcopy(d))
        return data

    m = {key: copy.deepcopy(request[key]) for key in INPUT_FIELDS}
    for field in ('config', 'policy'):
        data = read(m[field])
        require(hashlib.sha256(data).hexdigest() == binding[field + '_digest'], field + ' binding')
    require(digest(m['tables']) == binding['world_dump_digest'], 'original raw descriptor binding')
    require(digest(m['migrations']) == binding['migration_digest'], 'original ordered SQL binding')
    require(digest(m['dbc_files']) == binding['dbc_set_digest'], 'original DBC set binding')
    # Descriptor paths are part of these digests: preserve them by copying original files below.
    require(isinstance(m['tables'], dict), 'raw tables')
    tables = {name: jsonl(read(d), d) for name, d in m['tables'].items()}
    for group in ('migrations', 'dbc_files', 'source_files', 'locale_files'):
        require(isinstance(request[group], list), group + ' descriptors')
        require(len({d.get('name') for d in request[group]}) == len(request[group]), group + ' duplicate names')
        for d in request[group]:
            require(isinstance(d.get('name'), str) and bool(d['name']), group + ' name')
            read(d)
    require(bool(request['source_files']), 'source provenance files missing')
    require(bool(request['locale_files']), 'enabled locale custody missing (include explicit locale inventory)')
    attestation = decode(read(request['attestation']))
    require(isinstance(attestation, dict) and set(attestation) == {'snapshot_id', 'observation_generation',
        'binding_sha256', 'catalog_sha256', 'source_files_digest', 'locale_files_digest', 'exporter_revision',
        'loaded_generation_verified', 'config_sha256', 'phase'}, 'independent capture attestation fields')
    require(attestation['snapshot_id'] == binding['snapshot_id'] and attestation['binding_sha256'] == generation,
            'attestation generation')
    require(attestation['source_files_digest'] == digest(request['source_files']), 'source provenance binding')
    require(attestation['locale_files_digest'] == digest(request['locale_files']), 'locale provenance binding')
    require(attestation['config_sha256'] == binding['config_digest'], 'loaded config provenance binding')
    require(attestation['exporter_revision'] == digest(request['source_files']), 'exporter actual source bytes')
    require(type(attestation['loaded_generation_verified']) is bool, 'loaded identity must be explicit')
    catalog_raw = read(request['catalog'])
    require(hashlib.sha256(catalog_raw).hexdigest() == attestation['catalog_sha256'], 'original native catalogue attestation')
    catalog = decode(catalog_raw)
    slots = catalogue(catalog)
    require(attestation['observation_generation'] == catalog['observation_generation'] and attestation['phase'] == catalog['phase'],
            'native phase/observation provenance mismatch')
    policy = decode(read(request['policy']))
    require(policy.get('generation_snapshot') == binding['snapshot_id'], 'policy snapshot')
    require(isinstance(policy.get('stages'), list) and len(policy['stages']) == 37, 'full37 explicit stage policy required')
    require(policy['stages'][0].get('name') == catalog['phase']['phase_id'] and
            [s.get('id') for s in policy['stages'] if s.get('loaded')] == ['S01'], 'current native stage binding')
    require(isinstance(request['evidence'], dict), 'semantic evidence descriptors')
    proofs = {key: decode(read(d)) for key, d in request['evidence'].items()}
    for key, proof in proofs.items():
        require(isinstance(proof, dict) and set(proof) == {'snapshot_id', 'kind', 'source_revision', 'data'}, 'semantic evidence envelope: ' + key)
        require(proof['snapshot_id'] == binding['snapshot_id'], 'semantic evidence generation: ' + key)
        require(proof['kind'] in {'native_execution', 'accepted_policy', 'measurement', 'source_observation'}, 'semantic provenance kind: ' + key)
        require(proof['source_revision'] == attestation['exporter_revision'], 'semantic source provenance: ' + key)
    used_proofs = []

    def resolve(ref, field):
        require(isinstance(ref, dict) and set(ref) == {'document', 'path'}, 'missingnativefacts: ' + field + ' requires evidence reference')
        require(ref['document'] in proofs and isinstance(ref['path'], list), 'missingnativefacts: ' + field + ' proof')
        proof = proofs[ref['document']]
        field_name = field.rsplit('.', 1)[-1]
        if field.startswith('family.'):
            require(proof['kind'] in {'native_execution', 'source_observation'}, 'native semantic provenance: family source ' + field)
        elif field.startswith('item.') or field == 'raw disposition':
            require(proof['kind'] in {'native_execution', 'source_observation'}, 'projection disposition provenance: ' + field)
        elif field_name in {'travel', 'attempt', 'risk', 'contention'}:
            require(proof['kind'] in {'measurement', 'accepted_policy'}, 'missingnativefacts: measured/accepted policy ' + field)
        elif field_name in {'frontier', 'stages'}:
            require(proof['kind'] in {'native_execution', 'accepted_policy'}, 'native stage policy provenance: ' + field)
        else:
            require(proof['kind'] == 'native_execution', 'native semantic provenance: ' + field)
        value = proof['data']
        try:
            for part in ref['path']:
                if isinstance(value, list):
                    require(type(part) is int and 0 <= part < len(value), 'proof list index: ' + field)
                else:
                    require(isinstance(value, dict) and isinstance(part, str), 'proof object key: ' + field)
                value = value[part]
        except (KeyError, IndexError, TypeError) as error:
            raise CaptureError('missingnativefacts: ' + field + ' proof path') from error
        used_proofs.append(dict(field=field, reference=copy.deepcopy(ref), value_digest=digest(value)))
        return copy.deepcopy(value)

    semantic = decode(read(request['semantics']))
    require(isinstance(semantic, dict) and set(semantic) == {'snapshot_id', 'observation_generation',
        'quests', 'families', 'item_dispositions', 'row_dispositions', 'unresolved'}, 'semantic mapping shape')
    require(semantic['snapshot_id'] == binding['snapshot_id'] and semantic['observation_generation'] == catalog['observation_generation'], 'semantic observation generation')
    require(isinstance(semantic['quests'], dict) and set(semantic['quests']) == {s['key'] for s in slots},
            'missingnativefacts: exact native quest slot coverage')
    require(isinstance(semantic['families'], dict) and set(semantic['families']) == set(FAMILIES),
            'missingnativefacts: all14 family closure')
    def source_refs(refs, reason, nonempty=True):
        require(isinstance(refs, list) and (bool(refs) or not nonempty), reason + ' raw references')
        require(len({canonical(ref) for ref in refs}) == len(refs), reason + ' duplicate raw reference')
        for ref in refs:
            require(isinstance(ref, list) and len(ref) == 3 and isinstance(ref[0], str) and ref[0] in tables and
                    type(ref[1]) is int and 0 <= ref[1] < len(tables[ref[0]]), reason + ' raw reference')
            require(digest(tables[ref[0]][ref[1]]) == ref[2], reason + ' raw reference digest')

    families = {}; family_records = []
    item_ids = {item['entry'] for item in catalog['items']}
    for family, ref in semantic['families'].items():
        closure = resolve(ref, 'family.' + family)
        require(isinstance(closure, dict) and closure.get('status') in {'absent', 'observed', 'unresolved', 'unknown'} and
                isinstance(closure.get('reason'), str) and bool(closure['reason'].strip()),
                'missingnativefacts: family closure ' + family)
        simple = set(closure) == {'status', 'reason'}
        if closure['status'] == 'absent':
            require(simple and proofs[ref['document']]['kind'] == 'native_execution', 'native family absence provenance')
        elif family in {'quest_reward', 'dungeon_quest'} and closure['status'] == 'observed' and simple:
            require(proofs[ref['document']]['kind'] == 'native_execution', 'native quest family observation provenance')
        else:
            require(set(closure) == {'status', 'reason', 'raw_refs', 'known_facts', 'missing_reasons'},
                    'missingnativefacts: native getter required for observed family ' + family + '; precise unresolved family record required')
            source_refs(closure['raw_refs'], 'family ' + family, nonempty=closure['status'] != 'unknown')
            require(isinstance(closure['known_facts'], dict) and isinstance(closure['missing_reasons'], dict) and
                    bool(closure['missing_reasons']) and all(isinstance(field, str) and isinstance(reason, str) and bool(reason.strip())
                    for field, reason in closure['missing_reasons'].items()), 'native family unresolved reasons/known facts')
            targets = closure['known_facts'].get('item_entries', [])
            require(isinstance(targets, list) and all(type(item) is int and item in item_ids for item in targets),
                    'native family unresolved item references')
            family_records.append(dict(reason='native_family_unresolved', family=family,
                catalogue_sha256=attestation['catalog_sha256'], exporter_revision=attestation['exporter_revision'],
                snapshot_id=binding['snapshot_id'], observation_generation=catalog['observation_generation'],
                provenance_kind=proofs[ref['document']]['kind'], disposition=closure))
        families[family] = closure['status'] + ': ' + closure['reason']
    def raw_linkage(slot, refs):
        key = slot['key']
        require(isinstance(refs, list) and bool(refs), 'missingnativefacts: native raw row linkage ' + key)
        require(len({canonical(ref) for ref in refs}) == len(refs), 'duplicate native raw reference')
        linked_quest = False
        for ref in refs:
            require(isinstance(ref, list) and len(ref) == 3 and ref[0] in tables and type(ref[1]) is int and
                    0 <= ref[1] < len(tables[ref[0]]), 'native source raw reference')
            row = tables[ref[0]][ref[1]]
            require(digest(row) == ref[2], 'native source raw reference digest')
            identity = row.get('ID')
            if isinstance(identity, str) and identity.isascii() and identity.isdecimal():
                identity = int(identity)
            if ref[0] == 'quest_template' and type(identity) is int and identity == slot['source_entry']:
                linked_quest = True
        require(linked_quest, 'missingnativefacts: quest_template source identity linkage ' + key)

    facts = []; slot_dispositions = []
    stage_by_id = {stage.get('id'): stage for stage in policy['stages']}
    for slot in slots:
        key = slot['key']; refs = semantic['quests'][key]
        if isinstance(refs, dict) and set(refs) == {'disposition'}:
            disposition = resolve(refs['disposition'], key + '.disposition')
            require(isinstance(disposition, dict) and set(disposition) ==
                    {'status', 'native_slot', 'known_facts', 'missing_reasons'} and
                    disposition['status'] in {'future', 'unresolved'}, 'native noncurrent slot disposition shape')
            require(disposition['native_slot'] == slot, 'native disposed slot provenance')
            known = disposition['known_facts']; missing = disposition['missing_reasons']
            require(isinstance(known, dict) and {'family', 'stages', 'raw_refs'} <= set(known) <= FACT_FIELDS,
                    'noncurrent slot known facts')
            require(known['family'] in {None, 'quest_reward', 'dungeon_quest'}, 'disposed native family classification')
            stages = known['stages']
            require(isinstance(stages, list) and stages == sorted(set(stages)) and
                    all(isinstance(stage, str) and stage in stage_by_id for stage in stages),
                    'disposed slot requires exact known stage identities')
            require(disposition['status'] != 'future' or (bool(stages) and 'S01' not in stages), 'future requires known noncurrent stage identity')
            missing_stages = [stage for stage in stages if stage_by_id[stage].get('input_complete') is False]
            required_reasons = (REQUIRED_SEMANTICS - set(known)) | {'stage_inputs.' + stage for stage in missing_stages} | ({'stages'} if not stages else set()) | ({'family'} if known['family'] is None else set()) | ({'raw_refs'} if not known['raw_refs'] else set())
            require(isinstance(missing, dict) and required_reasons <= set(missing) and
                    all(isinstance(field, str) and isinstance(reason, str) and bool(reason.strip()) for field, reason in missing.items()),
                    'noncurrent slot precise missing-field/stage reasons')
            for field in {'key', 'source_entry', 'item_entry', 'minimum_count', 'maximum_count'} & set(known):
                require(canonical(known[field]) == canonical(slot[field]), 'native disposed known identity/yield provenance')
            if 'variant' in known:
                require(isinstance(known['variant'], dict), 'disposed semantic variant shape')
            if known['raw_refs']:
                raw_linkage(slot, known['raw_refs'])
            if known['family'] is not None:
                require(families[known['family']].startswith('observed:'), 'contradictory family closure')
            slot_dispositions.append(dict(reason='nonexecutable_native_slot', source=key,
                catalogue_sha256=attestation['catalog_sha256'], exporter_revision=attestation['exporter_revision'],
                snapshot_id=binding['snapshot_id'], observation_generation=catalog['observation_generation'],
                **disposition))
            continue
        require(isinstance(refs, dict) and set(refs) == REQUIRED_SEMANTICS | {'raw_refs'},
                'missingnativefacts: ' + key + ' exact mandatory fields ' + ','.join(sorted(REQUIRED_SEMANTICS | {'raw_refs'})))
        fact = {field: slot[field] for field in ('key', 'source_entry', 'item_entry', 'minimum_count', 'maximum_count')}
        fact.update({field: resolve(ref, key + '.' + field) for field, ref in refs.items()})
        require(fact['family'] in {'quest_reward', 'dungeon_quest'}, 'quest native family classification')
        require(fact['unresolved'] == [], 'missingnativefacts: unresolved mandatory acquisition semantics ' + key)
        require(fact['stages'] == ['S01'], 'only complete current-stage acquisition facts are executable; noncurrent slot requires disposition')
        raw_linkage(slot, fact['raw_refs'])
        reward = slot['native_reward']; suffix = str(reward['index'] + 1)
        item_column = ('RewardItem' if reward['kind']=='fixed' else 'RewardChoiceItemID') + suffix
        count_column = ('RewardAmount' if reward['kind']=='fixed' else 'RewardChoiceItemQuantity') + suffix
        linked_rows = [tables[ref[0]][ref[1]] for ref in fact['raw_refs'] if ref[0]=='quest_template']
        def original_integer(value):
            if isinstance(value,str) and value.isascii() and value.isdecimal():return int(value)
            return value
        require(any(original_integer(row.get(item_column))==reward['item'] and original_integer(row.get(count_column))==reward['count'] for row in linked_rows), 'qualified native SQL reward slot/count mismatch')

        # Opaque native slot/template custody is separate from real semantic variant predicates.
        require(isinstance(fact['variant'], dict), 'semantic variant shape')
        require(semantic['families'][fact['family']] is not None and families[fact['family']].startswith('observed:'),
                'contradictory family closure')
        facts.append(fact)
    observed = {f['family'] for f in facts} | {d['known_facts']['family'] for d in slot_dispositions} | {record['family'] for record in family_records}
    require(all(not status.startswith('observed:') or family in observed for family, status in families.items()), 'observed family missing native rows')
    sourced = {f['item_entry'] for f in facts}
    deferred_items = {d['native_slot']['item_entry'] for d in slot_dispositions}
    unresolved_family_items = {item for record in family_records for item in record['disposition']['known_facts'].get('item_entries', [])}
    require(isinstance(semantic['item_dispositions'], dict) and set(semantic['item_dispositions']) ==
            {str(item['entry']) for item in catalog['items'] if item['entry'] not in sourced | deferred_items | unresolved_family_items}, 'missingnativefacts: exact item dispositions')
    items = []
    for item in catalog['items']:
        if item['entry'] in sourced:
            reason = ''
        elif item['entry'] in deferred_items:
            reason = 'No executable current-stage source; retained nonexecutable native slots: ' + ','.join(d['source'] for d in slot_dispositions if d['native_slot']['item_entry'] == item['entry'])
        elif item['entry'] in unresolved_family_items:
            reason = 'No fully evidenced executable current source; retained unresolved native families: ' + ','.join(record['family'] for record in family_records if item['entry'] in record['disposition']['known_facts'].get('item_entries', []))
        else:
            reason = resolve(semantic['item_dispositions'][str(item['entry'])], 'item.' + str(item['entry']))
        require(isinstance(reason, str) and (item['entry'] in sourced or bool(reason)), 'missingnativefacts: item disposition reason')
        items.append(dict(entry=item['entry'], attributes={k: v for k, v in item.items() if k != 'entry'}, no_source_reason=reason))
    require(isinstance(semantic['row_dispositions'], list), 'row dispositions')
    dispositions = []
    for row in semantic['row_dispositions']:
        require(isinstance(row, dict) and set(row) == {'raw_ref', 'reason'}, 'row disposition mapping')
        dispositions.append(dict(raw_ref=row['raw_ref'], reason=resolve(row['reason'], 'raw disposition')))
    represented_refs = {tuple(ref) for fact in facts for ref in fact['raw_refs']}
    omitted_refs = {tuple(row['raw_ref']) for row in dispositions}
    deferred_refs = {}
    for disposition in slot_dispositions:
        for ref in disposition['known_facts']['raw_refs']:
            deferred_refs.setdefault(tuple(ref), []).append(disposition['source'])
    for record in family_records:
        for ref in record['disposition']['raw_refs']:
            deferred_refs.setdefault(tuple(ref), []).append('unresolved family ' + record['family'])
    for ref, sources in deferred_refs.items():
        if ref not in represented_refs | omitted_refs:
            dispositions.append(dict(raw_ref=list(ref), reason='Excluded from current executable projection; retained native source dispositions: ' + ','.join(sources)))
    unresolved = resolve(semantic['unresolved'], 'unresolved')
    require(isinstance(unresolved, list), 'native unresolved array')
    native = dict(schema='core1-native-acquisition-facts-v1', generation=generation,
        exporter_revision=attestation['exporter_revision'], raw_digest=binding['world_dump_digest'],
        loaded_generation_verified=attestation['loaded_generation_verified'], items=items,
        acquisitions=facts, dispositions=dispositions, family_dispositions=families, unresolved=unresolved + slot_dispositions + family_records,
        slot_custody=dict(catalogue=copy.deepcopy(request['catalog']), observation_generation=catalog['observation_generation'],
            slots=slots, source_revision=attestation['exporter_revision'], snapshot_id=binding['snapshot_id']))
    m['schema'] = 'core1-sealed-acquisition-v1'
    # Original relative basenames are copied unchanged, so original descriptor digests remain meaningful.
    files = {}
    for descriptor in descriptors:
        file_path = Path(descriptor['path'])
        if not file_path.is_absolute():
            require(file_path.name == str(file_path) and file_path.name not in {'native.json', 'input.json', 'custody.json'}, 'capture basename collision')
            data = read_descriptor(descriptor, path.parent)
            require(file_path.name not in files or files[file_path.name] == data, 'capture basename alias')
            files[file_path.name] = data
    require(not any(f['key'] in {x['key'] for x in facts[:i]} for i, f in enumerate(facts)), 'native key duplicate')
    custody = dict(schema='core1-native-capture-custody-v1', request_sha256=expected,
        input_generation=generation, catalogue_sha256=attestation['catalog_sha256'], attestation=attestation,
        descriptor_custody=descriptors, semantic_proofs=used_proofs, slot_dispositions=slot_dispositions, unresolved_families=family_records,
        missing_stages=[s for s in policy['stages'] if not s.get('input_complete')],
        native_provider_scope='loaded item/quest catalogue plus original admitted actor copies; other native family semantics remain explicitly unresolved',
        source_to_loaded_identity=attestation['loaded_generation_verified'], root_acceptance=False)
    return m, native, files, custody


def seal(request, expected, output):
    output = Path(output).absolute()
    require(not os.path.lexists(output), 'output must not exist')
    m, native, files, custody = prepare(request, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.capture-seal-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'sealed'; staged.mkdir()
        for name, data in files.items():
            (staged / name).write_bytes(data)
        native_raw = canonical(native) + b'\n'
        (staged / 'native.json').write_bytes(native_raw)
        m['native'] = dict(path='native.json', bytes=len(native_raw), sha256=hashlib.sha256(native_raw).hexdigest(), generation=m['binding']['snapshot_id'])
        manifest_raw = canonical(m) + b'\n'; manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        (staged / 'input.json').write_bytes(manifest_raw)
        loaded = load_inputs(staged / 'input.json', manifest_sha)
        _, report = producer.build(*loaded)
        custody['producer_preflight'] = {key: report[key] for key in ('selected_consumer_checks_passed', 'consumer_validation', 'consumer_incompatibilities', 'full37_complete')}
        custody['manifest_sha256'] = manifest_sha
        (staged / 'custody.json').write_bytes(canonical(custody) + b'\n')
        # Atomic no-replace directory publication on Linux; rename() could replace an empty race-created directory.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if rename(-100, os.fsencode(staged), -100, os.fsencode(output), 1):
            error = ctypes.get_errno(); raise OSError(error, os.strerror(error))
    return dict(manifest=str(output / 'input.json'), sha256=manifest_sha, root_acceptance=False)


# Raw source presence is an observation, never proof of effective native rates/conditions.
FAMILY_TABLES = {
    'quest_reward': ('quest_template',), 'dungeon_quest': ('quest_template',),
    'vendor': ('npc_vendor',), 'pvp_reward': ('npc_vendor',),
    'creature_loot': ('creature_loot_template', 'reference_loot_template'),
    'dungeon_drop': ('creature_loot_template', 'reference_loot_template'),
    'gameobject_loot': ('gameobject_loot_template', 'reference_loot_template'),
    'trainer_recipe': ('trainer_spell', 'npc_trainer'), 'craft': ('spell_dbc',),
    'disenchant': ('disenchant_loot_template',), 'prospecting': ('prospecting_loot_template',),
    'milling': ('milling_loot_template',), 'fishing': ('fishing_loot_template',),
    'skinning': ('skinning_loot_template',)}


def collect_current_evidence(observations_descriptor, identity_key, observed_quest_id, input_path, expected, output, *, _derived_files=None, _derived_receipts=()):
    """Off-tick automated assembly from an authenticated original callback observation descriptor.

    Never invokes native actor getters. capture_current_observations is the separate timed callback seam.
    Original receipt documents supply facts that current native getters do not expose.
    """
    input_path = Path(input_path).absolute(); output = Path(output).absolute()
    require(not os.path.lexists(output), 'output must not exist')
    raw = read_descriptor(dict(path=str(input_path), bytes=input_path.stat().st_size, sha256=expected), input_path.parent)
    source = decode(raw)
    require(isinstance(source, dict) and set(source) == INPUT_FIELDS | {'schema', 'catalog', 'attestation', 'source_files', 'locale_files', 'receipt_files'} and
            source['schema'] == 'core1-current-acquisition-input-v1', 'current collector input shape')
    binding = source['binding']; snapshot = binding['snapshot_id']
    require(set(binding) == BINDING_FIELDS and binding['compiler_revision'] == compiler_identity(), 'current collector compiler/binding')
    files = {}; derived = {} if _derived_files is None else _derived_files
    require(all(isinstance(name,str) and Path(name).name==name and name.startswith('native-derived-') and isinstance(data,bytes) and not (input_path.parent/name).exists() for name,data in derived.items()), 'derived native original basename collision')
    def read(descriptor):
        require(descriptor.get('generation') == snapshot, 'collector mixed descriptor generation')
        if descriptor['path'] in derived:
            data=derived[descriptor['path']]
            require(type(descriptor.get('bytes')) is int and len(data)==descriptor['bytes'] and hashlib.sha256(data).hexdigest()==descriptor.get('sha256'),'derived native byte custody')
        else:
            data = read_descriptor(descriptor, input_path.parent)
        if not Path(descriptor['path']).is_absolute():
            name = descriptor['path']
            require(name not in files or files[name] == data, 'collector capture basename alias')
            files[name] = data
        return data
    tables = {name: jsonl(read(descriptor), descriptor) for name, descriptor in source['tables'].items()}
    catalog_raw = read(source['catalog']); catalog = decode(catalog_raw); slots = catalogue(catalog)
    policy_raw = read(source['policy']); policy = decode(policy_raw); config_raw = read(source['config'])
    require(hashlib.sha256(policy_raw).hexdigest() == binding['policy_digest'] and
            hashlib.sha256(config_raw).hexdigest() == binding['config_digest'], 'collector original config/policy custody')
    require(digest(source['migrations']) == binding['migration_digest'] and digest(source['dbc_files']) == binding['dbc_set_digest'],
            'collector original SQL order/DBC custody')
    for group in ('migrations', 'dbc_files', 'source_files', 'locale_files'):
        for descriptor in source[group]: read(descriptor)
    revision = digest(source['source_files'])
    attestation = decode(read(source['attestation']))
    require(attestation['catalog_sha256'] == hashlib.sha256(catalog_raw).hexdigest() and
            attestation['binding_sha256'] == digest(binding) and attestation['exporter_revision'] == revision and
            attestation['phase'] == catalog['phase'] and attestation['observation_generation'] == catalog['observation_generation'] and
            attestation['source_files_digest'] == revision and attestation['locale_files_digest'] == digest(source['locale_files']) and
            attestation['config_sha256'] == binding['config_digest'] and type(attestation['loaded_generation_verified']) is bool,
            'collector original native/source generation binding')
    require(policy['generation_snapshot'] == snapshot and digest(source['tables']) == binding['world_dump_digest'], 'collector raw/policy binding')
    evidence = {}; receipts = []
    for descriptor in list(source['receipt_files']) + list(_derived_receipts):
        receipt = decode(read(descriptor))
        require(isinstance(receipt, dict) and set(receipt) == {'snapshot_id', 'kind', 'source_revision', 'data'} and
                receipt['snapshot_id'] == snapshot and receipt['source_revision'] == revision and
                receipt['kind'] in {'native_execution', 'measurement', 'accepted_policy'} and isinstance(receipt['data'], dict),
                'collector original policy/measurement/native receipt provenance')
        for section in ('slots', 'slot_dispositions'):
            require(isinstance(receipt['data'].get(section, {}), dict) and all(isinstance(value, dict) for value in receipt['data'].get(section, {}).values()),
                    'collector original receipt claim shape')
        name = 'receipt:' + descriptor['sha256']
        require(name not in evidence, 'collector duplicate original receipt')
        evidence[name] = copy.deepcopy(descriptor); receipts.append((name, receipt))
    # Preserve/authenticate the original callback capture; no actor call or disk census enters the timed tick.
    observation = decode(read(observations_descriptor))
    require(isinstance(observation, dict) and set(observation) == {'snapshot_id', 'kind', 'source_revision', 'data'} and
            observation['snapshot_id'] == snapshot and observation['kind'] == 'native_execution' and observation['source_revision'] == revision,
            'collector original callback observation provenance')
    observed_data = observation['data']
    require(isinstance(observed_data, dict) and set(observed_data) == {'catalogue_digest', 'observation_generation', 'identity_key', 'observed_quest_id', 'actor'} and
            observed_data['catalogue_digest'] == digest(catalog) and observed_data['observation_generation'] == catalog['observation_generation'] and
            observed_data['identity_key'] == identity_key and observed_data['observed_quest_id'] == observed_quest_id,
            'collector original callback/catalogue identity')
    actor = observed_data['actor']
    require(isinstance(actor, dict) and set(actor) == {'gear', 'resources', 'phase', 'scope'} and actor['phase'] == catalog['phase'],
            'collector original callback phase/shape')
    for row in (actor['gear'], actor['resources']):
        require(isinstance(row, dict) and row.get('identity_key') == identity_key and row.get('online') is True and
                row.get('control_allowed') is True and row.get('reason') == '', 'collector original callback admitted actor')
    require(all(actor['gear'].get(field) == actor['resources'].get(field) for field in ('account_id', 'character_guid', 'map_id', 'level')),
            'collector original callback actor/resource drift')
    evidence['original-callback'] = copy.deepcopy(observations_descriptor)
    def envelope(kind, data):
        return dict(snapshot_id=snapshot, kind=kind, source_revision=revision, data=data)
    # Keep float-bearing native values in their exact original byte-custodied document; never coerce Decimal.
    native = envelope('native_execution', dict(actor=dict(document='original-callback', path=['actor']), slots={}, families={}))
    observed = envelope('source_observation', dict(families={}, items={}, rows={}))
    policy_proof = envelope('accepted_policy', dict(original_stage_policy=policy, native_phase=actor['phase'],
        original_policy_sha256=source['policy']['sha256'], original_config_sha256=source['config']['sha256']))
    semantic = dict(snapshot_id=snapshot, observation_generation=catalog['observation_generation'], quests={}, families={},
        item_dispositions={}, row_dispositions=[], unresolved={})
    missing = {}; selected_families = set()
    def ref(document, path): return dict(document=document, path=path)
    def receipt_field(key, field):
        found = []
        for document, receipt in receipts:
            values = receipt['data'].get('slots', {}).get(key, {})
            if field in values:
                kind = receipt['kind']
                allowed = {'measurement', 'accepted_policy'} if field in {'travel', 'attempt', 'risk', 'contention'} else {'native_execution', 'accepted_policy'} if field in {'stages', 'frontier'} else {'native_execution'}
                require(kind in allowed, 'collector receipt field provenance: ' + key + '.' + field)
                found.append((ref(document, ['slots', key, field]), values[field]))
        require(len(found) <= 1, 'collector ambiguous receipt claim: ' + key + '.' + field)
        return found[0] if found else None
    for slot in slots:
        key = slot['key']
        linked = [[table, ordinal, digest(row)] for table, rows in tables.items() if table == 'quest_template'
                  for ordinal, row in enumerate(rows) if str(row.get('ID')) == str(slot['source_entry'])]
        known = dict(raw_refs=linked)
        native['data']['slots'][key] = known
        refs = {'raw_refs': ref('auto-native', ['slots', key, 'raw_refs'])}
        known_claims = {}
        for field in sorted(REQUIRED_SEMANTICS):
            claim = receipt_field(key, field)
            if claim is not None:
                refs[field] = claim[0]; known_claims[field] = claim[1]
        claim = receipt_field(key, 'raw_refs')
        if claim is not None:
            require(canonical(claim[1]) == canonical(linked), 'collector native/receipt raw linkage drift')
        disposition = [(document, receipt['data']['slot_dispositions'][key]) for document, receipt in receipts
            if receipt['kind'] == 'native_execution' and key in receipt['data'].get('slot_dispositions', {})]
        require(len(disposition) <= 1, 'collector ambiguous native slot disposition')
        if disposition:
            refs = dict(disposition=ref(disposition[0][0], ['slot_dispositions', key]))
            family = disposition[0][1]['known_facts']['family']
            if family is not None: selected_families.add(family)
        else:
            absent = sorted((REQUIRED_SEMANTICS | {'raw_refs'}) - set(refs))
            if not linked: absent.append('raw_refs')
            if absent:
                missing[key] = sorted(set(absent))
                retained = dict(known_claims, raw_refs=linked)
                retained.setdefault('family', None); retained.setdefault('stages', [])
                reasons = {field:'Original native/accepted policy/measurement evidence missing for '+field for field in missing[key]}
                for field in missing[key]:
                    original_reasons=[receipt['data']['source_missing'][key][field] for _,receipt in receipts if receipt['kind']=='native_execution' and isinstance(receipt['data'].get('source_missing'),dict) and key in receipt['data']['source_missing'] and field in receipt['data']['source_missing'][key]]
                    require(all(isinstance(reason,str) and reason for reason in original_reasons) and len(set(original_reasons))<=1,'collector original missing-reason ambiguity/type')
                    if original_reasons:reasons[field]=original_reasons[0]
                if not retained['stages']: reasons['stages']='Source current/future eligibility not established by global phase alone'
                if retained['family'] is None: reasons['family']='Native quest/dungeon classification not exported'
                for stage in retained['stages']:
                    if not policy['stages'][int(stage[1:])-1]['input_complete']:
                        reasons['stage_inputs.'+stage]=policy['stages'][int(stage[1:])-1]['reason']
                native['data'].setdefault('slot_dispositions', {})[key] = dict(status='unresolved',native_slot=slot,known_facts=retained,missing_reasons=reasons)
                refs = dict(disposition=ref('auto-native', ['slot_dispositions', key]))
            if known_claims.get('family') is not None: selected_families.add(known_claims['family'])
        semantic['quests'][key] = refs
    for family in FAMILIES:
        if family in selected_families:
            native['data']['families'][family] = dict(status='observed', reason='Native slot family classification retained from original pinned native execution receipt')
            semantic['families'][family] = ref('auto-native', ['families', family])
        else:
            refs = [[table, ordinal, digest(row)] for table in FAMILY_TABLES[family] for ordinal, row in enumerate(tables.get(table, []))]
            observed['data']['families'][family] = dict(status='observed' if refs else 'unknown',
                reason='Original raw source rows observed; loaded effective native semantics unknown' if refs else 'No native family export or native absence evidence available',
                raw_refs=refs, known_facts=dict(raw_table_rows={table: len(tables[table]) for table in FAMILY_TABLES[family] if table in tables}),
                missing_reasons=dict(native_semantics='Missing loaded native getter/effective predicate/rate evidence; raw presence does not establish native execution'))
            semantic['families'][family] = ref('auto-source', ['families', family])
    for item in catalog['items']:
        key = str(item['entry'])
        observed['data']['items'][key] = 'No fully evidenced executable current source in this capture; actual native acquisition availability remains unresolved in retained family/source records'
        # prepare() computes which items require a disposition; no all-family absence is inferred.
        semantic['item_dispositions'][key] = ref('auto-source', ['items', key])
    represented = {tuple(ref) for slot in native['data']['slots'].values() for ref in slot['raw_refs']}
    for table, rows in tables.items():
        for ordinal, row in enumerate(rows):
            raw_ref = [table, ordinal, digest(row)]
            if tuple(raw_ref) not in represented:
                key = table + ':' + str(ordinal)
                observed['data']['rows'][key] = 'Original row retained; native effective acquisition join/predicate unresolved, not a native exclusion or empty family'
                semantic['row_dispositions'].append(dict(raw_ref=raw_ref, reason=ref('auto-source', ['rows', key])))
    native['data']['unresolved'] = [dict(reason='automated_current_native_evidence_missing', source=key, fields=fields) for key, fields in missing.items()]
    semantic['unresolved'] = ref('auto-native', ['unresolved'])
    # Derive projection omission keys from genuinely complete current facts and explicit dispositions only.
    sourced = set(); deferred = set()
    for slot in slots:
        refs = semantic['quests'][slot['key']]
        if 'disposition' in refs:
            deferred.add(slot['item_entry'])
        elif slot['key'] not in missing:
            sourced.add(slot['item_entry'])
    semantic['item_dispositions'] = {key: value for key, value in semantic['item_dispositions'].items() if int(key) not in sourced | deferred}
    request = {key: copy.deepcopy(source[key]) for key in INPUT_FIELDS | {'catalog', 'attestation', 'source_files', 'locale_files'}}
    request.update(schema='core1-native-capture-request-v1', evidence=evidence)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.current-evidence-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'collected'; staged.mkdir()
        for name, data in files.items():
            require(not name.startswith('auto-'), 'collector reserved output basename')
            (staged / name).write_bytes(data)
        def descriptor(name, value):
            data = canonical(value) + b'\n'; (staged / name).write_bytes(data)
            return dict(path=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), generation=snapshot)
        for name, proof in (('auto-native', native), ('auto-source', observed), ('auto-policy', policy_proof)):
            request['evidence'][name] = descriptor(name + '.json', proof)
        request['semantics'] = descriptor('auto-semantics.json', semantic)
        request_raw = canonical(request) + b'\n'; (staged / 'auto-request.json').write_bytes(request_raw)
        request_sha = hashlib.sha256(request_raw).hexdigest()
        ready = False; refusal = None
        try:
            prepare(staged / 'auto-request.json', request_sha)
            ready = bool(prepare(staged / 'auto-request.json', request_sha)[1]['acquisitions'])
        except CaptureError as error:
            refusal = str(error)
        diagnostic = dict(schema='core1-automated-current-evidence-v1', input_sha256=expected,
            request_sha256=request_sha, ready_for_seal=ready, missing_current_native_facts=missing,
            refusal=refusal, original_receipts=[dict(document=name, kind=receipt['kind']) for name, receipt in receipts],
            actor_scope=actor['scope'], acquisition_measurements='original pinned measurement/accepted-policy receipts only; API latency never substituted',
            required_wiring='Call capture_current_observations inside existing population_bootstrap.population_tick; retain authenticated immutable original result, then run collect_current_evidence off-tick. Never call actor bindings via public SOAP catalogue seam',
            root_acceptance=False)
        descriptor('auto-collection.json', diagnostic)
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True); rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if rename(-100, os.fsencode(staged), -100, os.fsencode(output), 1):
            error = ctypes.get_errno(); raise OSError(error, os.strerror(error))
    return dict(request=str(output / 'auto-request.json'), sha256=request_sha, ready_for_seal=ready,
        missing_current_native_facts=missing, diagnostic=str(output / 'auto-collection.json'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--request', required=True); p.add_argument('--sha256', required=True)
    p.add_argument('--output', required=True); args = p.parse_args()
    try:
        sys.stdout.buffer.write(canonical(seal(args.request, args.sha256, args.output)) + b'\n'); return 0
    except Exception as error:
        sys.stderr.write('REFUSED: ' + str(error) + '\n'); return 2


if __name__ == '__main__':
    raise SystemExit(main())
