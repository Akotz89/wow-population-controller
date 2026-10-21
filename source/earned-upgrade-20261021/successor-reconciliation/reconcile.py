#!/usr/bin/env python3
"""Reconcile one already-running successor; write only new custody/observation receipts."""
import hashlib
import json
import os
from pathlib import Path
import sys
import types

HERE = Path(__file__).resolve().parent
PINS_SHA = '9f1ab57d5e87782cc02cdf978d74b16c0bc619afb7b0677d4c0788e5a8b0a521'
HELPER = HERE.parent / 'successor-activation/successor.py'
if hashlib.sha256(HELPER.read_bytes()).hexdigest() != '460c3c109c95f63d86a05ddfceff230fb3dcfaa2311d4d22030fef2350ec4cd4':
    raise RuntimeError('active-successor-helper-drift')
sys.path.insert(0, str(HELPER.parent))
import successor as s

a = s.a


def pins():
    a.regular(HERE / 'pins.json')
    a.need(a.sha(HERE / 'pins.json') == PINS_SHA, 'reconciliation-pins-drift')
    return json.loads((HERE / 'pins.json').read_bytes())


def snapshot(path):
    before = a.regular(path)
    raw = path.read_bytes()
    after = a.regular(path)
    keys = ('st_dev','st_ino','st_mode','st_uid','st_gid','st_size','st_mtime_ns','st_ctime_ns')
    a.need(all(getattr(before,key) == getattr(after,key) for key in keys), 'metadata-read-raced')
    return raw


def transport(library):
    """Compile the two existing pinned transport originals without trusting CLI pyc."""
    expected = {'ingest_capture':'5b55cb0bdb4e5806b904539bef234ac0f7afa63e3ecd092e8dde0da8016695bb',
                'capture_native_acquisition':'3cfa72f7cbbc7e8b2647ab9dbf8fa8fbea7abee2ba1cef125d4dcb740f214968'}
    previous = sys.modules.get('ingest_capture')
    try:
        for name, digest in expected.items():
            path = library / (name + '.py'); raw = snapshot(path)
            a.need(s.digest(raw) == digest, 'transport-source-drift')
            module = types.ModuleType(name); module.__file__ = str(path)
            if name == 'ingest_capture': sys.modules[name] = module
            exec(compile(raw, str(path), 'exec'), module.__dict__)
        return module.NativeCaptureFrames, module.parse_transport
    finally:
        if previous is None: sys.modules.pop('ingest_capture',None)
        else: sys.modules['ingest_capture'] = previous


class Linux(s.Linux):
    def script_mounts(self, owned):
        lines = (Path('/proc') / str(owned['process']['pid']) / 'mountinfo').read_text().splitlines()
        for path in (a.NEW / 'python', a.NEW / 'population/python'):
            found = [line.split() for line in lines if len(line.split()) > 6 and line.split()[4] == str(path)]
            dev = path.stat().st_dev
            a.need(len(found) == 1 and found[0][3] == str(path)
                   and found[0][2] == f'{os.major(dev)}:{os.minor(dev)}'
                   and 'ro' in found[0][5].split(','), 'script-mount-drift')

    def startup_journal(self, current):
        invocation = current['props']['InvocationID']; pid = str(current['process']['pid'])
        raw = a.command(['journalctl', '_SYSTEMD_INVOCATION_ID='+invocation, '_PID='+pid,
                         '--output=json', '--no-pager'])
        publication = "Published 2 Python scripts from canonical root '" + str(a.NEW / 'population/python/witness_scripts') + "'"
        source = 'source sha256:' + pins()['source_identity']
        kept = []
        for line in raw.splitlines():
            row = json.loads(line); message = row.get('MESSAGE','')
            if publication in message or source in message and '(worldserver-daemon) ready' in message:
                a.need(row.get('_PID') == pid and row.get('_SYSTEMD_INVOCATION_ID') == invocation, 'startup-journal-owner-drift')
                a.need(int(row['__MONOTONIC_TIMESTAMP']) >= int(current['exit_custody']['ExecMainStartTimestampMonotonic']), 'startup-journal-epoch-drift')
                kept.append({key:row[key] for key in ('MESSAGE','_PID','_SYSTEMD_INVOCATION_ID','__MONOTONIC_TIMESTAMP','__REALTIME_TIMESTAMP')})
        a.need(any(publication in row['MESSAGE'] for row in kept)
               and any(source in row['MESSAGE'] and '(worldserver-daemon) ready' in row['MESSAGE'] for row in kept), 'startup-publication-journal-absent')
        return kept

    def owned_log_prefix(self, current, expected=None):
        log = pins()['startup_log']; path = a.NEW / log['relative_path']
        identity = (log['device'],log['inode']); root = Path('/proc') / str(current['process']['pid'])
        def owner():
            a.need(a.same_owner(self.world(a.NEW,a.NEW_SHA),current)
                   and self.exit_identity() == current['exit_custody'], 'owned-startup-log-process-drift')
            st = a.regular(path)
            a.need((st.st_dev,st.st_ino) == identity and st.st_size >= log['prefix_bytes'], 'owned-startup-log-identity-drift')
            fd = root / 'fd' / log['fd']
            a.need(os.readlink(fd) == str(path) and (fd.stat().st_dev,fd.stat().st_ino) == identity, 'owned-startup-log-fd-drift')
            flags = next(line.split()[1] for line in (root/'fdinfo'/log['fd']).read_text().splitlines() if line.startswith('flags:'))
            a.need(int(flags,8) & 3 != 0, 'owned-startup-log-fd-not-writable')
            # A second writer to this one log inode would make its provenance unknown.
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit() or entry == root: continue
                try:
                    for other in (entry/'fd').iterdir():
                        try:
                            info = dict(line.split(':',1) for line in (entry/'fdinfo'/other.name).read_text().splitlines()
                                        if line.startswith(('flags:','ino:')))
                            if int(info['flags'],8) & 3 == 0 or int(info['ino']) != identity[1]: continue
                            target = other.stat()
                            a.need((target.st_dev,target.st_ino) != identity, 'owned-startup-log-foreign-writer')
                        except FileNotFoundError: continue
                except FileNotFoundError: continue
            return {'device':st.st_dev,'inode':st.st_ino,'mode':st.st_mode & 0o7777,'uid':st.st_uid,'gid':st.st_gid,
                    'daemon_fd':log['fd'],'daemon_fd_flags':flags}
        before = owner()
        # A fixed observed prefix makes ordinary later appends compatible with retry/custody.
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            a.need((opened.st_dev,opened.st_ino) == identity, 'owned-startup-log-open-raced')
            raw = stream.read(log['prefix_bytes'])
            a.need(len(raw) == log['prefix_bytes'], 'owned-startup-log-prefix-short')
            stream.seek(0)
            a.need(stream.read(log['prefix_bytes']) == raw, 'owned-startup-log-prefix-rewritten')
        a.need(owner() == before, 'owned-startup-log-owner-changed')
        records = {}
        offset = 0
        for line in raw.splitlines(keepends=True):
            text = line.rstrip(b'\r\n').decode('utf-8')
            for name in ('source_header','publication','ready'):
                if log[name] in text and not (name == 'source_header' and log['ready'] in text):
                    a.need(name not in records, 'owned-startup-log-duplicate-record')
                    records[name] = {'offset':offset,'line':text}
            offset += len(line)
        a.need(set(records) == {'source_header','publication','ready'} and
               records['source_header']['offset'] < records['publication']['offset'] < records['ready']['offset'], 'owned-startup-log-records-absent')
        value = dict(before,path=str(path),prefix_bytes=len(raw),prefix_sha256=s.digest(raw),records=records)
        if expected is not None: a.need(value == expected, 'owned-startup-log-prefix-or-owner-rewritten')
        return value

    def namespace(self):
        _, parse = transport(a.NEW / 'population/python/lib')
        emit = "__import__('capture_native_acquisition').NativeCaptureFrames.emit("
        head = ("dict(root=str(__script_root__),ready=ENGINE_READY,startups=ENGINE_STARTUPS,"
                "config_sha=_config_sha,config_generation=_witness.config['strategic_generation'],"
                "mode=_witness.mode,armed=_witness.strategy.acquisition_capture is not None,"
                "population_callable=callable(population_tick),"
                "dependencies={n:getattr(__import__('sys').modules[n],'_witness_source_sha256',None) for n in _names})")
        def read(expression):
            value = a.soap_command('python exec '+emit+expression+')',
                a.read_credentials('/etc/azerothcore/cpi188-cast.env'),timeout=20)
            a.need(value['ok'], 'namespace-transport-soap-refused')
            return parse(value['response'])
        before = read(head)
        status = read('dict(status=witness_status())')
        a.need(read(head) == before, 'namespace-changed-during-read')
        a.need(set(status) == {'status'}, 'namespace-status-envelope')
        return dict(before, status=status['status'])


def live(backend, saved):
    s.recheck(backend, saved, live=False)
    p = pins()
    a.need(saved['config']['sha256'] == p['config_sha256'] and saved['config']['generation'] == p['config_generation'], 'specific-config-binding-drift')
    a.need(saved['config']['dependencies'] == {k+'.py':v for k,v in p['dependencies'].items()}, 'specific-dependency-binding-drift')
    a.need(s.roster_binding() == saved['config']['roster'], 'accepted-roster-drift')
    s.check_drop(dict(saved['baseline']['drop'],sha256=s.digest(s.successor_drop(saved['baseline'],p['config_sha256']))))
    current = backend.world(a.NEW,a.NEW_SHA)
    a.need(current['process'] == p['process'], 'specific-process-drift')
    props = current['props']; oldprops = saved['baseline']['world']['props']
    a.need(props['MainPID'] == str(p['process']['pid']) and props['InvocationID'] == p['invocation']
           and props['NRestarts'] == '0' and props['ActiveState'] == 'active' and props['SubState'] == 'running', 'specific-unit-drift')
    for key in a.CONFIG_PROPS:
        expected = oldprops[key].replace(str(a.OLD),str(a.NEW)) if key in ('WorkingDirectory','BindReadOnlyPaths') else oldprops[key]
        a.need(props[key] == expected, 'specific-service-property-drift')
    a.need('path='+str(a.NEW/'bin/worldserver')+' ;' in props['ExecStart']
           and 'argv[]='+str(a.NEW/'bin/worldserver')+' -c '+str(a.NEW/'etc/worldserver.conf')+' ;' in props['ExecStart'], 'specific-unit-exec-drift')
    epoch = backend.exit_identity()
    a.need(epoch['ExecMainPID'] == str(p['process']['pid']) and epoch['InvocationID'] == p['invocation'], 'specific-start-identity-drift')
    start_us = int(epoch['ExecMainStartTimestampMonotonic']); hz = os.sysconf('SC_CLK_TCK')
    a.need(saved['baseline']['world']['process']['startticks'] * 1000000 // hz < start_us
           <= (p['process']['startticks']+1) * 1000000 // hz, 'specific-start-epoch-drift')
    current['exit_custody'] = epoch
    backend.verify_bindings(a.NEW,current); backend.script_mounts(current)
    backend.writers(); idle = backend.idle()
    return current, idle


def startup_evidence(backend, current, saved):
    try:
        return {'kind':'invocation-journal','journal':backend.startup_journal(current)}
    except a.Refusal as error:
        if str(error) != 'startup-publication-journal-absent': raise
    lines = [line.strip() for line in (a.NEW/'etc/worldserver.conf').read_text().splitlines()
             if a.re.match(r'^\s*LogsDir\s*=',line)]
    a.need(lines == ['LogsDir = "'+str(a.NEW/'logs')+'"'] and 'logs' not in saved['candidate'], 'fresh-stage-log-basis-drift')
    return {'kind':'owned-server-log-prefix','journal_publication_pair_absent':True,
            'journal_absence_code':'startup-publication-journal-absent',
            'journal_query':{'pid':current['process']['pid'],'invocation':current['props']['InvocationID']},
            'stage_basis':{'helper_sha256':saved['performer'],'new_log_root':str(a.NEW/'logs'),
                'empty_log_base':'derived from pinned stage routine excluding old logs and creating this new directory',
                'contemporaneous_staged_log_inventory_retained':False},
            'ownership_scope':'current exact PID/birth/invocation holds the same writable log inode; log lines have no invocation metadata',
            'log':backend.owned_log_prefix(current)}


def verify_startup(backend, current, evidence):
    if evidence['kind'] == 'owned-server-log-prefix':
        backend.owned_log_prefix(current,evidence['log'])


def namespace_valid(value):
    p = pins()
    a.need(value['root'] == str(a.NEW/'population/python/witness_scripts') and value['ready'] is True
           and type(value['startups']) is int and value['startups'] == 1 and value['config_sha'] == p['config_sha256']
           and type(value['config_generation']) is int and value['config_generation'] == p['config_generation'] and value['dependencies'] == p['dependencies']
           and value['mode'] == 'observe' and value['armed'] is False and value['population_callable'] is True, 'specific-namespace-drift')
    status = value['status']; earned = status['earned']
    a.need(status['mode'] == 'observe' and status['reason'] == 'selected_actor_offline'
           and status['scheduler_eligible'] is False and earned['identity_key'] == p['actor']['identity_key']
           and earned['character_guid'] == p['actor']['character_guid'] and earned['online'] is False
           and earned['session_requested'] is False and status['capture'] is None and status['custody_ack'] is None
           and status['required_acks'] == status['applied_acks'] == [], 'specific-witness-state-drift')


def retain(path, raw):
    if path.exists() or path.is_symlink():
        a.need(snapshot(path) == raw, 'existing-reconciliation-output-drift')
        return False
    a.write(path,raw)
    return True


def reconcile(backend):
    p = pins()
    a.regular(HELPER); a.regular(HELPER.parent/'pins.json')
    a.need(a.sha(HELPER) == p['helper_sha256'] and a.sha(HELPER.parent/'pins.json') == p['helper_pins_sha256'], 'active-helper-drift')
    saved = s.saved_stage(backend)
    files = ('staged.private.json','stage-owner.private.json','cutover.private.json','cutover-result.private.json','rollback-dropin.private')
    originals = {name:snapshot(s.RUN/name) for name in files}
    a.need(json.loads(originals['staged.private.json']) == saved, 'staged-snapshot-drift')
    a.need(json.loads(originals['cutover-result.private.json']) == p['failed_result'], 'failed-cutover-lineage-drift')
    attempt = json.loads(originals['cutover.private.json'])
    a.need(attempt == {'before':saved['baseline']['world'],'start_requested':False}, 'cutover-baseline-lineage-drift')
    owner = json.loads(originals['stage-owner.private.json'])
    a.need(owner == {'performer':p['helper_sha256'],'pins':p['helper_pins_sha256'],
                    'old':str(a.OLD),'new':str(a.NEW),'baseline':saved['baseline']}, 'stage-owner-lineage-drift')
    a.need((s.RUN/'cutover.private.json').stat().st_mtime_ns <= (s.RUN/'cutover-result.private.json').stat().st_mtime_ns, 'cutover-file-lineage-order')
    current, idle = live(backend,saved)
    startup = startup_evidence(backend,current,saved)
    python = backend.soap('python status'); info = backend.soap('server info')
    a.need('Python Engine: Ready' in python and 'Reloading: No' in python
           and 'Configured Scripts Path: '+str(a.NEW/'population/python/witness_scripts') in python, 'actual-python-not-ready')
    a.need('source sha256:'+p['source_identity']+';' in info
           and 'Connected players: 0. Characters in world: 0.' in info, 'actual-server-source-or-idle-drift')
    namespace = backend.namespace(); namespace_valid(namespace)
    a.need(live(backend,saved) == (current,idle), 'reconciliation-live-identity-changed')
    verify_startup(backend,current,startup)
    for name, raw in originals.items(): a.need(snapshot(s.RUN/name) == raw, 'original-metadata-changed')
    observation = {'schema':'core1-successor-post-failure-observation-v1','world':current,'idle':idle,
        'python_status':python,'server_source_line':info.splitlines()[0],'server_source_identity':p['source_identity'],'server_connected_players':0,
        'server_characters_in_world':0,'startup_evidence':startup,'namespace':namespace,
        'original_cutover':p['failed_result'],'successor_loaded_acceptance':False,'gameplay_verified':False}
    observation_raw = a.canonical(observation); custody_raw = a.canonical(current)
    receipt = {'schema':'core1-specific-successor-adoption-v1','companion_sha256':a.sha(Path(__file__)),
        'pins_sha256':PINS_SHA,'active_helper_sha256':p['helper_sha256'],
        'original_metadata_sha256':{name:s.digest(raw) for name,raw in originals.items()},
        'observation_sha256':s.digest(observation_raw),'custody_sha256':s.digest(custody_raw),
        'specific_process':p['process'],'invocation':p['invocation'],'existing_process_custody_reconciled':True,
        'original_cutover_activated':False,'successor_loaded_acceptance':False,'gameplay_verified':False,
        'service_action':False,'configuration_change':False,'actor_action':False,'database_mutation':False}
    # Each new file is atomic/exclusive. Exact existing bytes permit an interrupted/idempotent retry.
    outputs = ((s.RUN/'post-failure-observation.private.json',observation_raw),
               (s.RUN/'candidate-custody.private.json',custody_raw),
               (s.RUN/'successor-adoption.private.json',a.canonical(receipt)))
    for path,raw in outputs:
        if path.exists() or path.is_symlink(): a.need(snapshot(path) == raw, 'existing-reconciliation-output-drift')
    for path,raw in outputs:
        a.need(live(backend,saved) == (current,idle), 'reconciliation-live-identity-changed')
        verify_startup(backend,current,startup)
        retain(path,raw)
    a.need(live(backend,saved) == (current,idle), 'reconciliation-live-identity-changed')
    verify_startup(backend,current,startup)
    for name,raw in originals.items(): a.need(snapshot(s.RUN/name) == raw, 'original-metadata-changed')
    return {'reconciled':True,'specific_pid':p['process']['pid'],'invocation':p['invocation'],
            'custody_sha256':receipt['custody_sha256'],'original_cutover_activated':False,
            'successor_loaded_acceptance':False,'gameplay_verified':False,'service_action':False,'actor_action':False}


def main():
    try:
        a.need(os.geteuid() == 0 and s.RUN.is_dir() and s.RUN.resolve() == s.RUN
               and s.RUN.stat().st_mode & 0o077 == 0, 'root-private-successor-run-required')
        backend = Linux()
        with backend.lock(): result = reconcile(backend)
        print(json.dumps(result,sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'error':a.safe_error(error)},sort_keys=True)); return 1

if __name__ == '__main__': raise SystemExit(main())
