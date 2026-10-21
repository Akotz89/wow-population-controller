#!/usr/bin/env python3
"""Finite read-only world-content collection and OFF-TICK custody assembly."""
import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

WORLD = 'cpi225_world_v7'
CONTAINER = 'azerothcore-mysql-cpi225-v7'
REMOTE_ROOT = Path('/srv/wow/work/core1-native-earned-upgrade-delivery-20261021')
DBC = dict(areatable_dbc='AreaTable.dbc', item_dbc='Item.dbc',
           itemextendedcost_dbc='ItemExtendedCost.dbc', lock_dbc='Lock.dbc',
           map_dbc='Map.dbc', skillline_dbc='SkillLine.dbc',
           skilllineability_dbc='SkillLineAbility.dbc', spell_dbc='Spell.dbc')
CONTENT = set('''item_template quest_template quest_template_addon quest_offer_reward
quest_request_items quest_details quest_greeting quest_poi quest_poi_points
quest_mail_sender quest_reward_choice_items quest_reward_item
creature creature_template creature_template_addon creature_addon creature_equip_template
creature_loot_template creature_queststarter creature_questender creature_template_movement
creature_template_model creature_formations creature_onkill_reputation
gameobject gameobject_template gameobject_addon gameobject_queststarter gameobject_questender
gameobject_loot_template reference_loot_template item_loot_template disenchant_loot_template
prospecting_loot_template milling_loot_template fishing_loot_template skinning_loot_template
pickpocketing_loot_template npc_vendor npc_trainer trainer trainer_spell creature_default_trainer
conditions disables game_event game_event_creature game_event_gameobject game_event_quest
game_event_npc_vendor game_event_pool game_event_creature_quest game_event_gameobject_quest
pool_template pool_creature pool_gameobject pool_pool pool_quest
spawn_group spawn_group_template instance_spawn_groups
spell_area spell_required spell_script_names spell_linked_spell spell_learn_spell spell_group
smart_scripts waypoints waypoint_data gossip_menu gossip_menu_option areatrigger_teleport
access_requirement achievement_reward instance_template item_enchantment_template
version updates updates_include'''.split()) | set(DBC)
REQUIRED = {'item_template', 'quest_template', 'creature_template', 'creature',
            'gameobject_template', 'gameobject', 'npc_vendor', 'conditions', 'updates'} | set(DBC)
SAFE_CONFIG = set('''DataDir DBC.Locale Locale MaxPlayerLevel Quest.LowLevelHideDiff
Quest.HighLevelHideDiff Rate.Drop.Item.Poor Rate.Drop.Item.Normal Rate.Drop.Item.Uncommon
Rate.Drop.Item.Rare Rate.Drop.Item.Epic Rate.Drop.Item.Legendary Rate.Drop.Item.Artifact
Rate.Drop.Item.Referenced Rate.Drop.Item.ReferencedAmount Rate.Drop.Money Rate.XP.Quest
Rate.XP.Kill Rate.XP.Explore Rate.Reputation.Gain Rate.Skill.Discovery Rate.Skill.Drop
Python.Enabled Python.ScriptsPath Python.Population.Enabled Python.Population.EntryScript
Python.Population.TickIntervalMs Python.Population.TickBudgetMs Updates.EnableDatabases
ProgressionSystem.Enable ProgressionSystem.LoadScripts ProgressionSystem.LoadDatabase
ProgressionSystem.ReapplyUpdates ProgressionSystem.DisabledAttunements
ProgressionSystem.AuditedLegacyAdoption ProgressionSystem.SharedPhaseGate.Enable
ProgressionSystem.SharedPhaseGate.Enabled ProgressionSystem.SharedPhaseGate.CurrentPhase ProgressionSystem.SharedPhaseGate.AdminBypass
AiPlayerbot.AutoEquipUpgradeLoot AiPlayerbot.RandomBotAutologin AiPlayerbot.MaxRandomBots'''.split())
IDENTIFIER = re.compile(r'^[A-Za-z0-9_]+$')


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(canonical(value))


def original(path):
    path = Path(path).absolute()
    require(path.is_file() and not path.is_symlink() and
            not any(p.is_symlink() for p in path.parents), 'original must be regular nonsymlink file: ' + str(path))
    return path.read_bytes()


def descriptor(path, snapshot, **extra):
    raw = original(path)
    return dict(path=str(Path(path).absolute()), bytes=len(raw), sha256=sha(raw),
                generation=snapshot, **extra)


def read(desc, root, snapshot):
    require(desc.get('generation') == snapshot, 'mixed descriptor generation')
    path = Path(desc['path'])
    if not path.is_absolute():
        require(path.name == desc['path'] and path.name not in {'.', '..'}, 'descriptor requires basename or absolute path')
        path = root / path
    raw = original(path)
    require(type(desc.get('bytes')) is int and len(raw) == desc['bytes'] and sha(raw) == desc['sha256'],
            'original byte custody drift: ' + str(path))
    return raw


def write(root, filename, raw, snapshot, **extra):
    require(Path(filename).name == filename, 'output basename')
    path = root / filename
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return dict(path=filename, bytes=len(raw), sha256=sha(raw), generation=snapshot, **extra)


def document(root, filename, value, snapshot, **extra):
    return write(root, filename, canonical(value) + b'\n', snapshot, **extra)


def command(argv, data=None):
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    # Never echo credential-bearing stderr or arbitrary process output.
    require(result.returncode == 0, 'subprocess refused (' + argv[0] + ', exit ' + str(result.returncode) + ')')
    return result.stdout


def mysql(sql):
    return command(['sudo', '-n', 'podman', 'exec', '-i', CONTAINER, 'bash', '-c',
                    'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql --user=root '
                    '--default-character-set=utf8mb4 --batch --raw --skip-column-names ' + WORLD], sql.encode())


def configured_projection(paths):
    sources, values = [], {}
    for path in paths:
        raw = original(path)
        sources.append(dict(path=str(Path(path).absolute()), bytes=len(raw), sha256=sha(raw)))
        for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
            match = re.match(r'^\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$', line)
            if not match:
                continue
            key, value = match.groups()
            if key not in SAFE_CONFIG and not re.fullmatch(r'ProgressionSystem\.Bracket_[A-Za-z0-9_]+', key):
                continue
            lexer = shlex.shlex(value, posix=True); lexer.whitespace_split = True; lexer.commenters = '#'
            value = ' '.join(lexer)
            require('\x00' not in value, 'invalid selected configuration value')
            values[key] = dict(value=value, source_sha256=sha(raw), line=number)
    return dict(schema='core1-sanitized-config-projection-v1', sources=sources, values=values,
                source_order='caller-provided actual load order; last selected assignment retained',
                limitation='Selected configured values only. Defaults, overrides and loaded effective values require independent runtime evidence. No credentials copied.')


def mapped_module(path, snapshot, expected_sha256, maps):
    path = Path(path).absolute()
    require(not path.is_symlink() and not any(p.is_symlink() for p in path.parents),
            'native module must have nonsymlink file ancestry')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        status = os.fstat(stream.fileno())
        require(stat.S_ISREG(status.st_mode), 'native module must be a regular file')
        checksum = sha(stream.read())
        identity = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        require(identity(status) == identity(os.fstat(stream.fileno())) == identity(path.stat(follow_symlinks=False)),
                'native module file changed while hashing')
    require(checksum == expected_sha256, 'expected native module hash drift')
    rows = [line.split(None,5) for line in maps.splitlines()]
    rows = [row for row in rows if len(row) == 6 and row[5] in {str(path),str(path)+' (deleted)'}]
    require(rows and all(row[5] == str(path) for row in rows), 'expected native module is not mapped or was deleted')
    device = (os.major(status.st_dev),os.minor(status.st_dev))
    require(all(tuple(int(part,16) for part in row[3].split(':')) == device and int(row[4]) == status.st_ino for row in rows),
            'mapped native module device/inode differs from hashed regular file')
    module = dict(path=str(path),bytes=status.st_size,sha256=checksum,generation=snapshot)
    mapping = dict(path=str(path),device_major=device[0],device_minor=device[1],inode=status.st_ino,
                   mapping_count=len(rows),scope='Mapped device/inode equals hashed regular file; process-memory bytes were not hashed')
    return module,mapping


def runtime_guard(args):
    values = dict(line.split('=', 1) for line in command(['systemctl', 'show', args.unit,
                  '--property=MainPID,InvocationID,ActiveState']).decode().splitlines())
    require(values == dict(MainPID=str(args.pid), InvocationID=args.invocation, ActiveState='active'),
            'expected service PID/invocation drift')
    proc = Path('/proc') / str(args.pid)
    ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    exe_link = os.readlink(proc / 'exe')
    require(not exe_link.endswith(' (deleted)'), 'running executable was deleted')
    exe = descriptor(exe_link, args.snapshot)
    require(exe['sha256'] == args.exe_sha256 and sha((proc / 'exe').read_bytes()) == args.exe_sha256,
            'expected loaded executable hash drift')
    maps = (proc / 'maps').read_text()
    module,mapping = mapped_module(args.module,args.snapshot,args.module_sha256,maps)
    return dict(pid=args.pid, invocation=args.invocation, start_ticks=ticks, executable=exe,
                module=module, module_mapping=mapping, config=configured_projection(args.config))


def schema_inventory():
    sql = "SELECT JSON_OBJECT('TABLE_NAME',c.TABLE_NAME,'COLUMN_NAME',c.COLUMN_NAME," \
          "'ORDINAL_POSITION',c.ORDINAL_POSITION,'DATA_TYPE',c.DATA_TYPE,'COLUMN_TYPE',c.COLUMN_TYPE," \
          "'ENGINE',t.ENGINE) FROM information_schema.COLUMNS c JOIN information_schema.TABLES t " \
          "ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME " \
          "WHERE c.TABLE_SCHEMA=DATABASE() ORDER BY c.TABLE_NAME,c.ORDINAL_POSITION;"
    groups = {}
    for line in mysql(sql).splitlines():
        row = json.loads(line)
        if row['TABLE_NAME'] in CONTENT or re.fullmatch(r'(item|quest|creature|gameobject)_template_locale', row['TABLE_NAME']):
            require(IDENTIFIER.fullmatch(row['COLUMN_NAME']) is not None, 'unsupported SQL column identifier')
            groups.setdefault(row['TABLE_NAME'], []).append(row)
    require(REQUIRED <= set(groups), 'required world content tables missing: ' + ','.join(sorted(REQUIRED - set(groups))))
    require(all(r['ENGINE'] == 'InnoDB' for rows in groups.values() for r in rows),
            'consistent read-only content capture requires InnoDB tables')
    return groups


def export_sql(groups):
    statements = ['SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;',
                  'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;']
    for table, rows in groups.items():
        pairs = []
        for row in rows:
            col = '`' + row['COLUMN_NAME'] + '`'
            kind = row['DATA_TYPE'].lower()
            require(kind not in {'binary', 'varbinary', 'blob', 'tinyblob', 'mediumblob', 'longblob', 'bit'},
                    'binary SQL content requires explicit lossless representation: ' + table)
            value = 'CAST(' + col + ' AS CHAR)' if kind in {'float', 'double', 'decimal'} else col
            pairs += ["'" + row['COLUMN_NAME'] + "'", value]
        # DBC SQL loader uses native ID DESC; other row order is the actual SELECT order, never relabelled native load order.
        order = ' ORDER BY `ID` DESC' if table in DBC else ''
        statements.append("SELECT JSON_OBJECT('table','" + table + "','row',JSON_OBJECT(" + ','.join(pairs) +
                          ')) FROM `' + table + '`' + order + ';')
    statements.append('COMMIT;')
    return '\n'.join(statements)


def checksums(groups):
    raw = mysql('CHECKSUM TABLE ' + ','.join('`' + t + '`' for t in groups) + ' EXTENDED;')
    values = [line.decode().split('\t') for line in raw.splitlines()]
    require(len(values) == len(groups) and all(len(r) == 2 and r[1].isdigit() for r in values),
            'world content checksum unavailable')
    return dict(values)


def sql_records(inventory):
    """Native progression identity ledger rows are metadata, never SQL files."""
    records=[]
    for index,row in enumerate(inventory):
        name=row.get('name'); expected=row.get('hash','')
        require(name != '__progression_incomplete__', 'actual incomplete progression SQL marker is present')
        if isinstance(name,str) and name.startswith('__progression_native__'):
            identity=name[len('__progression_native__'):]
            require(re.fullmatch('[a-fA-F0-9]{64}',identity) and row.get('state') == 'MODULE' and
                    expected == identity[:40], 'malformed original progression native ledger history')
            continue
        require(isinstance(name,str) and Path(name).name == name and name.endswith('.sql') and
                re.fullmatch('[a-fA-F0-9]{40}',expected), 'applied SQL inventory lacks original SQL name/SHA1')
        records.append((index,row))
    return records


def collect(args):
    output = Path(args.output).absolute()
    require(REMOTE_ROOT in output.parents and not output.exists() and
            not any(p.is_symlink() for p in output.parents), 'new output must be inside owned remote run root')
    require(args.config and args.snapshot and args.pid > 1 and re.fullmatch('[a-f0-9]{32}', args.invocation), 'explicit capture identity required')
    exporter = descriptor(__file__,args.snapshot)
    before = runtime_guard(args)
    groups = schema_inventory()
    before_world = checksums(groups)
    output.mkdir(mode=0o700)
    handles, tables = {}, {}
    try:
        for table, schema in groups.items():
            handles[table] = (output / (table + '.jsonl')).open('xb')
            tables[table] = dict(columns=[r['COLUMN_NAME'] for r in schema], rows=0, schema=schema)
        # Stream the single transaction: do not retain an entire world export in RAM.
        with tempfile.TemporaryFile() as sql:
            sql.write(export_sql(groups).encode()); sql.seek(0)
            process = subprocess.Popen(['sudo', '-n', 'podman', 'exec', '-i', CONTAINER, 'bash', '-c',
                'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql --user=root --default-character-set=utf8mb4 '
                '--batch --raw --skip-column-names ' + WORLD], stdin=sql, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                for line in process.stdout:
                    record = json.loads(line); table = record['table']; row = record['row']
                    require(set(row) == set(tables[table]['columns']), 'SQL row/column drift')
                    handles[table].write(canonical(row) + b'\n'); tables[table]['rows'] += 1
                require(process.wait(timeout=1800) == 0, 'world transaction failed')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
        for handle in handles.values():
            handle.flush(); os.fsync(handle.fileno()); handle.close()
        for table, meta in list(tables.items()):
            tables[table] = dict(descriptor(output / (table + '.jsonl'), args.snapshot,
                                 columns=meta['columns'], rows=meta['rows'], schema=meta['schema']), path=table + '.jsonl')
        dbcs = []
        for name in DBC.values():
            dbcs.append(write(output, name, original(Path(args.dbc_root) / name), args.snapshot, name=name))
        config = document(output, 'config-projection.json', before['config'], args.snapshot)
        locale = dict(schema='core1-locale-file-inventory-v1', base_dbc_root=str(Path(args.dbc_root).absolute()),
                      configured_locale={k:v for k,v in before['config']['values'].items() if k in {'DBC.Locale','Locale'}},
                      files=[], limitation='Filesystem availability and configured locale only; native availableDbcLocales mask not observed.')
        locales = []
        for directory in sorted(Path(args.dbc_root).iterdir()):
            if not re.fullmatch('[a-z]{2}[A-Z]{2}', directory.name):
                continue
            require(directory.is_dir() and not directory.is_symlink(), 'locale directory symlink/type')
            for name in DBC.values():
                if (directory / name).exists():
                    d = write(output, directory.name + '-' + name, original(directory / name), args.snapshot,
                              name=directory.name + '/' + name)
                    locales.append(d); locale['files'].append(d)
        locales.insert(0, document(output, 'locale-inventory.json', locale, args.snapshot, name='locale-inventory'))
        inventory = [json.loads(line) for line in original(output / 'updates.jsonl').splitlines() if line]
        inventory.sort(key=lambda row: (str(row.get('timestamp', '')), str(row.get('name', ''))))
        by_name = {}
        for path in Path(args.sql_source_root).rglob('*.sql'):
            if path.is_file():
                by_name.setdefault(path.name, []).append(path)
        migrations, missing = [], []
        for index, row in sql_records(inventory):
            name = row.get('name'); expected = row.get('hash')
            matches = [(p, original(p)) for p in by_name.get(name, [])]
            matches = [(p, raw) for p, raw in matches if hashlib.sha1(raw).hexdigest().lower() == expected.lower()]
            if not matches:
                missing.append(dict(inventory_ordinal=index, name=name, applied_sha1=expected)); continue
            require(len({sha(raw) for _,raw in matches}) == 1, 'ambiguous applied SQL originals')
            migrations.append(write(output, 'sql-' + str(index).zfill(6) + '.sql', matches[0][1], args.snapshot,
                name=name, ordinal=len(migrations), inventory_ordinal=index, applied_record=row,
                source_path=str(matches[0][0].absolute()), historical_execution_order_verified=False))
        limitations = dict(schema='core1-applied-sql-custody-v1', original_inventory=inventory,
            ordering='Observed updates.timestamp then name; timestamps/ties/archive/base imports do not prove historical execution order.',
            historical_execution_order_verified=False, missing_originals=missing,
            source_root=str(Path(args.sql_source_root).absolute()))
        sql_meta = document(output, 'applied-sql-custody.json', limitations, args.snapshot)
        after = runtime_guard(args)
        require(before == after and groups == schema_inventory() and before_world == checksums(groups),
                'runtime/config/schema/world changed during immutable capture; coordinate a fresh run')
        for d in dbcs:
            require(sha(original(Path(args.dbc_root) / d['name'])) == d['sha256'], 'DBC changed during capture')
        for d in locales[1:]:
            require(sha(original(Path(args.dbc_root) / d['name'])) == d['sha256'], 'locale DBC changed during capture')
        require(descriptor(__file__,args.snapshot) == exporter, 'exporter source changed during capture')
        capture = dict(schema='core1-physical-world-capture-v1', snapshot_id=args.snapshot,
            captured_at=datetime.now(timezone.utc).isoformat().replace('+00:00','Z'), runtime=before,
            world_database=WORLD, tables=tables, config=config, migrations=migrations,
            dbc_files=dbcs, locale_files=locales, sql_custody=sql_meta, world_checksums=before_world,
            exporter=exporter, complete_original_sql=not missing)
        result = document(output, 'capture.json', capture, args.snapshot)
        print(json.dumps(result, sort_keys=True))
    finally:
        for handle in handles.values():
            handle.close()
    # An interrupted directory has no capture.json and cannot be assembled; retain it for diagnosis.


def frozen_modules(candidate):
    sys.path.insert(0, str(candidate))
    import capture_manifest
    from effective_data import compiler_identity
    return capture_manifest, compiler_identity


def source_closure(candidate, snapshot, extras):
    mapping = candidate.parent / 'evidence/native/CURRENT-HANDLER-SOURCE-MAP.json'
    raw = original(mapping)
    from first_current_input import HANDLER_SOURCE_MAP_SHA
    require(sha(raw) == HANDLER_SOURCE_MAP_SHA, 'original handler map hash drift')
    proof = json.loads(raw)
    required = [dict(path=str(mapping), sha256=sha(raw)), proof['selected_vfs']]
    handlers = proof['known_overriding_handlers'] + proof['source_verified_default_only_handlers']
    required += handlers + proof['support_sources']
    required += [dict(path=r['constructor_path'], sha256=r['constructor_sha256']) for r in handlers if 'constructor_path' in r]
    paths = {}
    for row in required:
        path = Path(row['path']).absolute(); d = descriptor(path,snapshot,name=str(path))
        require(d['sha256'] == row['sha256'], 'original handler source drift: ' + str(path))
        paths[str(path)] = d
    from ordinary_kill_proof import census_pin, census_sources
    census_descriptor, census = census_pin()
    for row in [census_descriptor] + census_sources(census):
        path = Path(row['path']).absolute(); d = descriptor(path, snapshot, name=str(path))
        require(d['sha256'] == row['sha256'] and d['bytes'] == row['bytes'], 'ordinary census source drift: '+str(path))
        paths[str(path)] = d
    compiler_files = sorted((candidate / 'producer').rglob('*.py')) + sorted((candidate / 'producer/schemas').glob('*.json'))
    compiler_files += [candidate/'producer/EIGHT-STORE-MAP.json']
    for path in [Path(__file__).absolute()] + sorted(candidate.glob('*.py')) + compiler_files + extras:
        path = Path(path).absolute()
        d = descriptor(path, snapshot, name=str(path))
        require(str(path) not in paths or paths[str(path)] == d, 'source changed while recording closure: '+str(path))
        paths[str(path)] = d
    return [paths[name] for name in sorted(paths)]


def stage_policy(candidate, snapshot, catalog):
    header = candidate/'core/modules/mod-progression-system/src/ProgressionSystem.h'
    text = original(header).decode('utf-8')
    match = re.search(r'ProgressionBracketsNames\s*=\s*\{([^}]+)\}',text,re.S)
    require(match is not None, 'original progression stage mapping missing')
    names = re.findall(r'"([A-Za-z0-9_]+)"',match.group(1))
    require(len(names) == 39 and names[0] == '0' and names[-1] == 'Custom' and
            len(set(names)) == 39 and catalog['phase']['phase_id'] == names[1] == '1_19',
            'original current-only 37-stage mapping changed')
    return dict(generation_snapshot=snapshot,stages=[dict(id='S%02d'%i,name=name,loaded=i==1,
        input_complete=i==1,reason='' if i==1 else 'No original physical input snapshot collected for this future stage')
        for i,name in enumerate(names[1:-1],1)])


def validate(path, expected, candidate):
    capture, compiler = frozen_modules(candidate)
    path = Path(path).absolute(); raw = original(path)
    require(sha(raw) == expected, 'input manifest hash drift')
    inputs = json.loads(raw)
    require(set(inputs) == capture.INPUT_FIELDS | {'schema','catalog','attestation','source_files','locale_files','receipt_files'}
            and inputs['schema'] == 'core1-current-acquisition-input-v1', 'exact current input shape')
    b = inputs['binding']; snapshot = b['snapshot_id']
    require(set(b) == capture.BINDING_FIELDS and b['compiler_revision'] == compiler(), 'exact binding/compiler identity')
    require(REQUIRED <= set(inputs['tables']) and {d['name'] for d in inputs['dbc_files']} == set(DBC.values()),
            'physical content/eight DBC originals missing')
    for group in ('tables',):
        for name, d in inputs[group].items():
            capture.jsonl(read(d,path.parent,snapshot),d)
    for key in ('policy','config','catalog','attestation'):
        read(inputs[key],path.parent,snapshot)
    for key in ('migrations','dbc_files','source_files','locale_files','receipt_files'):
        require(len({d.get('name',d['path']) for d in inputs[key]}) == len(inputs[key]), 'duplicate descriptor identity')
        for d in inputs[key]:
            read(d,path.parent,snapshot)
    for key in ('policy','config'):
        require(inputs[key]['sha256'] == b[key+'_digest'], 'original config/policy binding')
    for key,field in (('tables','world_dump_digest'),('migrations','migration_digest'),('dbc_files','dbc_set_digest')):
        require(digest(inputs[key]) == b[field], 'descriptor set binding: '+key)
    require([d['ordinal'] for d in inputs['migrations']] == list(range(len(inputs['migrations']))), 'SQL inventory ordinal')
    a = json.loads(read(inputs['attestation'],path.parent,snapshot)); catalog = json.loads(read(inputs['catalog'],path.parent,snapshot))
    capture.catalogue(catalog)
    require(set(a) == {'snapshot_id','observation_generation','binding_sha256','catalog_sha256','source_files_digest',
            'locale_files_digest','exporter_revision','loaded_generation_verified','config_sha256','phase'}, 'attestation exact fields')
    require(a['snapshot_id'] == snapshot and a['binding_sha256'] == digest(b) and a['catalog_sha256'] == inputs['catalog']['sha256']
        and a['source_files_digest'] == digest(inputs['source_files']) and a['exporter_revision'] == digest(inputs['source_files'])
        and a['locale_files_digest'] == digest(inputs['locale_files']) and a['config_sha256'] == b['config_digest']
        and a['phase'] == catalog['phase'] and a['observation_generation'] == catalog['observation_generation']
        and type(a['loaded_generation_verified']) is bool, 'attestation original/source/phase drift')
    policy = json.loads(read(inputs['policy'],path.parent,snapshot))
    require(set(policy) == {'generation_snapshot','stages'} and policy['generation_snapshot'] == snapshot, 'stage policy generation')
    require([s['id'] for s in policy['stages']] == ['S%02d'%i for i in range(1,38)] and
            [s['id'] for s in policy['stages'] if s['loaded']] == ['S01'] and policy['stages'][0]['name'] == catalog['phase']['phase_id'], '37-stage current-only policy')
    for stage in policy['stages']:
        require(set(stage) == {'id','name','loaded','input_complete','reason'} and type(stage['loaded']) is bool and
                type(stage['input_complete']) is bool and isinstance(stage['reason'],str) and
                (stage['input_complete'] or stage['reason']) and (not stage['loaded'] or stage['input_complete']), 'stage input evidence/reason')
    require(not any(s['input_complete'] for s in policy['stages'][1:]), 'unknown future stages cannot be declared complete')
    return dict(manifest=str(path), sha256=expected, exporter_revision=digest(inputs['source_files']),
                compiler_revision=compiler(), loaded_generation_verified=a['loaded_generation_verified'], root_acceptance=False)


def canonical_candidate_id(value):
    return 'candidate:' + value if re.fullmatch('[0-9a-f]{64}', value) else value


def loaded_identity(evidence, binding, sources, locales):
    """Root's true observation applies only to its enumerated binding/source identity."""
    value = evidence['loaded_generation_verified']
    require(type(value) is bool, 'loaded identity must be explicit')
    if value:
        expected = dict(binding_sha256=digest(binding), source_files_digest=digest(sources),
                        exporter_revision=digest(sources), locale_files_digest=digest(locales),
                        config_sha256=binding['config_digest'])
        require(all(evidence.get(key) == val for key, val in expected.items()),
                'Root loaded-generation evidence does not attest this exact derivative binding/source/config/locale identity')
    return value


def assemble(args):
    candidate = Path(args.candidate).absolute()
    frozen_modules(candidate)
    capture_path = Path(args.capture).absolute(); remote = json.loads(original(capture_path))
    snapshot = remote['snapshot_id']; root = capture_path.parent
    require(remote['schema'] == 'core1-physical-world-capture-v1', 'physical capture schema')
    output = Path(args.output).absolute()
    require(not output.exists() and not any(p.is_symlink() for p in output.parents), 'new nonsymlink output required')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.physical-',dir=output.parent) as temp:
        staged = Path(temp) / 'input'; staged.mkdir()
        inputs = {key:remote[key] for key in ('tables','config','migrations','dbc_files','locale_files')}
        for group in [list(inputs['tables'].values()), [inputs['config']], inputs['migrations'], inputs['dbc_files'], inputs['locale_files'], [remote['sql_custody']]]:
            for d in group:
                raw = read(d,root,snapshot)
                require(not Path(d['path']).is_absolute(), 'physical collector originals must be basenames')
                write(staged,d['path'],raw,snapshot)
        sql_custody = json.loads(read(remote['sql_custody'],root,snapshot))
        originals = {d['inventory_ordinal']:d for d in inputs['migrations']}
        local_sql = {}
        for directory in args.sql_original_root:
            for path in Path(directory).rglob('*.sql'):
                local_sql.setdefault(path.name,[]).append(path)
        ordered = []
        for ordinal,(index,row) in enumerate(sql_records(sql_custody['original_inventory'])):
            expected = row.get('hash','').lower(); name = row.get('name')
            if index in originals:
                d = originals[index]; raw = read(d,root,snapshot)
                require(d['name'] == name and hashlib.sha1(raw).hexdigest() == expected, 'applied SQL original inventory mismatch')
                d = dict(d,ordinal=ordinal)
            else:
                matches = [(p,original(p)) for p in local_sql.get(name,[])]
                matches = [(p,raw) for p,raw in matches if hashlib.sha1(raw).hexdigest() == expected]
                require(matches and len({sha(raw) for _,raw in matches}) == 1, 'missing/ambiguous applied SQL original: '+str(name))
                d = write(staged,'sql-'+str(index).zfill(6)+'.sql',matches[0][1],snapshot,name=name,ordinal=ordinal,
                    inventory_ordinal=index,applied_record=row,source_path=str(matches[0][0].absolute()),historical_execution_order_verified=False)
            ordered.append(d)
        inputs['migrations'] = ordered
        catalog_raw = original(args.catalog); catalog = json.loads(catalog_raw)
        inputs['catalog'] = write(staged,'catalogue.json',catalog_raw,snapshot)
        policy_raw = original(args.policy) if args.policy else canonical(stage_policy(candidate,snapshot,catalog))+b'\n'
        inputs['policy'] = write(staged,'stage-policy.json',policy_raw,snapshot)
        sources = source_closure(candidate,snapshot,[Path(p) for p in args.source_extra])
        # Bind the remote exporter original too, so downloaded collection cannot silently use another collector.
        require(remote['exporter']['sha256'] == sha(original(__file__)), 'remote exporter differs from assembler source')
        inputs['source_files'] = sources
        inputs['receipt_files'] = [descriptor(p,snapshot) for p in args.receipt]
        revisions = json.loads(original(args.revisions))
        require(set(revisions) == {'candidate_id','core_revision','playerbots_revision','progression_revision','content_revision'}, 'actual revision identities required')
        require(all(isinstance(v,str) and v for v in revisions.values()), 'empty revision identity')
        revisions['candidate_id'] = canonical_candidate_id(revisions['candidate_id'])
        from effective_data import compiler_identity
        binding = dict(revisions,compiler_revision=compiler_identity(),captured_at=remote['captured_at'],snapshot_id=snapshot,
            config_digest=inputs['config']['sha256'],policy_digest=inputs['policy']['sha256'],world_dump_digest=digest(inputs['tables']),
            dbc_set_digest=digest(inputs['dbc_files']),migration_digest=digest(inputs['migrations']))
        evidence = json.loads(original(args.loaded_evidence))
        require(evidence.get('snapshot_id') == snapshot and type(evidence.get('loaded_generation_verified')) is bool and
                evidence.get('runtime') == remote['runtime'] and evidence.get('phase') == catalog['phase'] and
                evidence.get('observation_generation') == catalog['observation_generation'] and
                evidence.get('catalog_sha256') == sha(catalog_raw), 'Root loaded-generation evidence disagrees with physical capture/catalogue')
        require(isinstance(evidence.get('evidence_files'),list) and evidence['evidence_files'], 'Root original loaded evidence descriptors required')
        for d in evidence['evidence_files']:
            read(d,Path(args.loaded_evidence).absolute().parent,snapshot)
        attestation = dict(snapshot_id=snapshot,observation_generation=catalog['observation_generation'],binding_sha256=digest(binding),
            catalog_sha256=sha(catalog_raw),source_files_digest=digest(sources),locale_files_digest=digest(inputs['locale_files']),
            exporter_revision=digest(sources),loaded_generation_verified=loaded_identity(evidence,binding,sources,inputs['locale_files']),
            config_sha256=inputs['config']['sha256'],phase=catalog['phase'])
        inputs['attestation'] = document(staged,'attestation.json',attestation,snapshot)
        inputs.update(schema='core1-current-acquisition-input-v1',binding=binding)
        document(staged,'loaded-generation-evidence.json',evidence,snapshot)
        document(staged,'physical-capture.json',remote,snapshot)
        manifest = document(staged,'input.json',inputs,snapshot)
        validate(staged/'input.json',manifest['sha256'],candidate)
        # All original sources must still match immediately before publication.
        for d in sources:
            read(d,staged,snapshot)
        # Same Linux no-replace primitive as the existing collector; never replace a concurrent output.
        libc = ctypes.CDLL(None,use_errno=True); rename = libc.renameat2
        rename.argtypes = [ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        if rename(-100,os.fsencode(staged),-100,os.fsencode(output),1):
            code = ctypes.get_errno(); raise OSError(code,os.strerror(code))
    result = validate(output/'input.json',manifest['sha256'],candidate)
    print(json.dumps(result,sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    collect_parser = sub.add_parser('collect',help='run on server-01 only; no services or DB writes')
    for name in ('snapshot','invocation','exe-sha256','module','module-sha256','dbc-root','sql-source-root','output','unit'):
        collect_parser.add_argument('--'+name,required=True)
    collect_parser.add_argument('--pid',type=int,required=True)
    collect_parser.add_argument('--config',action='append',required=True)
    assemble_parser = sub.add_parser('assemble',help='run OFF TICK in WSL with frozen candidate originals')
    for name in ('capture','catalog','loaded-evidence','revisions','candidate','output'):
        assemble_parser.add_argument('--'+name,required=True)
    assemble_parser.add_argument('--policy',help='existing exact original policy; otherwise derive 37 names from pinned original ProgressionSystem.h')
    assemble_parser.add_argument('--source-extra',action='append',default=[])
    assemble_parser.add_argument('--receipt',action='append',default=[])
    assemble_parser.add_argument('--sql-original-root',action='append',default=[],help='WSL originals for missing applied SQL; SHA1 must match actual updates row')
    validate_parser = sub.add_parser('validate')
    for name in ('input','sha256','candidate'):
        validate_parser.add_argument('--'+name,required=True)
    args = parser.parse_args()
    try:
        if args.action == 'collect': collect(args)
        elif args.action == 'assemble': assemble(args)
        else: print(json.dumps(validate(args.input,args.sha256,Path(args.candidate)),sort_keys=True))
    except (Refused,OSError,KeyError,ValueError,TypeError,subprocess.SubprocessError) as error:
        # Errors identify boundaries; never dump arbitrary source bytes/configuration or child stderr.
        print('REFUSED: ' + str(error),file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
