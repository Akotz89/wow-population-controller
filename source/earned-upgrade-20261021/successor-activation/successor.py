#!/usr/bin/env python3
"""Pinned successor staging/cutover; existing performer and private custody stay immutable."""
import argparse
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import sys
import tarfile

HERE = Path(__file__).resolve().parent
PINS_SHA = '5c7692ba7ba7353e6cf702dad21156f4d6ba8c6e7f63b9f3e8542378887af740'
HELPERS = {'activate.py': 'bc9b37c314d26b60160de7796c9801fe68594f57c470cb1b59fb97858e31a9f0',
           'soap_transport.py': 'c33e1468a222eef0c90b9841fb7d83236444b1533f503bfd0d64a7f73b5eb3c7'}

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

for name, expected in HELPERS.items():
    source = HERE.parent / 'activation' / name
    if source.is_symlink() or digest(source.read_bytes()) != expected:
        raise RuntimeError('reviewed-helper-drift')
sys.path.insert(0, str(HERE.parent / 'activation'))
spec = importlib.util.spec_from_file_location('_core1_predecessor_helpers', HERE.parent / 'activation/activate.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
# Module-local constants only; the predecessor source and records are never written.
a.OLD = Path('/srv/wow/releases/cpi225-native-earned-upgrade-20261021')
a.NEW = Path('/srv/wow/releases/cpi225-native-held-custody-20261021')
a.OLD_SHA = '0e1fd05b210d4ba080c9d0f07a81cd7ebe3feba8686a1d212c9b31b7eeb5934d'
a.NEW_SHA = 'cbaa4a4ba3bcf83785444f90567d6d2a61bf390d5fa1594f79c038413fa585ce'
RUN = Path('/srv/wow/work/core1-native-earned-upgrade-delivery-20261021/successor')


def pins():
    a.need(a.sha(HERE / 'pins.json') == PINS_SHA, 'successor-pins-drift')
    return json.loads((HERE / 'pins.json').read_bytes())


def package_pins():
    value = pins()
    return {'package': value['package'], 'members': value['members']}
a.package_pins = package_pins


def inventory(root):
    return {key: value for key, value in a.tree(root).items()
            if (key != 'logs' and not key.startswith('logs/') and '__pycache__' not in Path(key).parts
                and not key.endswith('.pyc')) or 'link' in value}


def drop_snapshot():
    s = a.regular(a.DROP)
    return {'sha256': a.sha(a.DROP), 'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'gid': s.st_gid}


def check_drop(expected):
    a.need(drop_snapshot() == expected, 'owned-dropin-drift')


def successor_drop(baseline, config_sha):
    entries = shlex.split(baseline['world']['props']['BindReadOnlyPaths'])
    old = str(a.OLD / 'etc/modules') + ':/srv/wow/server/etc/modules'
    a.need(sum(row.removesuffix(':rbind') == old for row in entries) == 1, 'current-module-bind-absent')
    raw = (RUN / 'rollback-dropin.private').read_bytes()
    a.need(digest(raw) == baseline['drop']['sha256'], 'rollback-dropin-copy-drift')
    a.need(raw.count(('ExecStart=' + str(a.OLD / 'bin/worldserver')).encode()) == 1, 'dropin-exec-owner')
    updated = raw.replace(str(a.OLD).encode(), str(a.NEW).encode())
    return updated.rstrip(b'\n') + (f'\nEnvironment="PBGUIDE_WITNESS_CONFIG_PATH={a.NEW}/witness-config.private.json"\n'
            f'Environment="PBGUIDE_WITNESS_CONFIG_SHA256={config_sha}"\n').encode()



class Linux(a.Linux):
    def writers(self):
        super().writers()
        ancestors = {os.getpid()}
        parent = os.getppid()
        while parent > 1:
            ancestors.add(parent)
            try:
                parent = int((Path('/proc') / str(parent) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
            except FileNotFoundError:
                break
        # The pinned predecessor guard lacks these two successor/rollback code paths.
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) in ancestors:
                continue
            try:
                for fd in (entry / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                        if target == str(a.NEW / 'bin/worldserver') or target.startswith(str(a.OLD / 'population/python') + '/'):
                            flags = next(line.split()[1] for line in (entry / 'fdinfo' / fd.name).read_text().splitlines()
                                         if line.startswith('flags:'))
                            a.need(int(flags, 8) & 3 == 0, 'open-code-config-writer')
                    except FileNotFoundError:
                        continue
            except FileNotFoundError:
                continue

    def original(self):
        a.original_executable()
        current = self.world(a.OLD, a.OLD_SHA)
        a.need(current['process'] and current['props']['ActiveState'] == 'active'
               and current['props']['NRestarts'] == '0', 'current-world-not-owned-active')
        custody = json.loads((a.RUN / 'candidate-custody.private.json').read_bytes())
        a.need(a.same_owner(current, custody) and self.exit_identity() == custody['exit_custody']
               and custody['process']['pid'] == 1660628 and custody['process']['startticks'] == 83958589
               and custody['props']['InvocationID'] == '69d97eee2bad484ab368f2885b975cf6', 'predecessor-custody-drift')
        self.verify_bindings(a.OLD, current)
        self.writers()
        return {'world': current, 'deps': self.deps(), 'current': inventory(a.OLD),
                'policy': a.tree(a.BASE / 'policy'), 'service_files': self.service_files(),
                'drop': drop_snapshot(), 'idle': self.idle()}

    def unchanged(self, baseline):
        a.original_executable()
        a.need(inventory(a.OLD) == baseline['current'], 'rollback-release-drift')
        a.need(a.tree(a.BASE / 'policy') == baseline['policy'], 'policy-drift')
        a.need(self.deps() == baseline['deps'], 'dependency-drift')
        a.need(self.service_files() == baseline['service_files'], 'service-files-drift')

    def executable_preflight(self, baseline):
        target = a.NEW / 'bin/worldserver'
        s = a.regular(target)
        expected = pins()['members']['bin/worldserver']
        a.need(s.st_size == expected['bytes'] and stat.S_IMODE(s.st_mode) == int(expected['mode'], 8)
               and a.sha(target) == expected['sha256'], 'preflight-executable-drift')
        props = baseline['world']['props']
        user = pwd.getpwnam(props['User']) if props['User'] else pwd.getpwuid(0)
        gid = grp.getgrnam(props['Group']).gr_gid if props['Group'] else user.pw_gid
        prefix = ['setpriv', '--reuid', str(user.pw_uid), '--regid', str(gid), '--init-groups', '--']
        a.command(prefix + ['test', '-x', str(target)])
        elf = a.command(['readelf', '-h', str(target)])
        a.need('ELF64' in elf and 'X86-64' in elf, 'preflight-elf-abi')
        dependencies = a.command(prefix + ['ldd', str(target)])
        a.need('not found' not in dependencies, 'preflight-missing-dependency')
        # Accepted Main.cpp's help path returns before config/database/listener startup.
        help_text = a.command(prefix + [str(target), '--help'])
        a.need('help' in help_text.lower() and 'config' in help_text.lower(), 'preflight-help-unsupported')
        config = a.NEW / 'witness-config.private.json'
        if config.exists():
            descriptor = json.loads(config.read_bytes())['seed_catalogue']
            for source in (config, Path(descriptor['path'])):
                a.command(prefix + ['test', '-r', str(source)])
        a.need(a.sha(target) == expected['sha256'], 'preflight-executable-changed')
        return {'sha256': expected['sha256'], 'bytes': s.st_size, 'mode': stat.S_IMODE(s.st_mode),
                'service_uid': user.pw_uid, 'service_gid': gid, 'elf': elf, 'ldd': dependencies,
                'help': help_text, 'help_argv': [str(target), '--help'], 'database_started': False}

    def python_observation(self, owned):
        a.need(a.same_owner(self.world(a.NEW, a.NEW_SHA), owned), 'observation-process-drift')
        code = ("print('CPI225_SUCCESSOR ' + __import__('json').dumps(dict(root=str(__script_root__),"
                "ready=ENGINE_READY,startups=ENGINE_STARTUPS,config_sha=_config_sha,"
                "config_generation=_witness.config['strategic_generation'],mode=_witness.mode,"
                "armed=_witness.strategy.acquisition_capture is not None,"
                "population_callable=callable(population_tick),"
                "dependencies={n:getattr(__import__('sys').modules[n],'_witness_source_sha256',None) for n in _names},"
                "status=witness_status()),sort_keys=True))")
        value = a.soap_command('python exec ' + code, a.read_credentials('/etc/azerothcore/cpi188-cast.env'), timeout=20)
        a.need(value['ok'], 'python-observation-soap-fault')
        match = a.re.search(r'CPI225_SUCCESSOR (\{[^\r\n]*\})', value['response'])
        a.need(match is not None, 'python-observation-absent')
        observed = json.loads(match[1])
        a.need(observed['ready'] is True and observed['startups'] == 1
               and observed['population_callable'] is True and observed['mode'] == 'observe'
               and observed['armed'] is False, 'python-observation-startup-refused')
        a.need(a.same_owner(self.world(a.NEW, a.NEW_SHA), owned), 'observation-process-drift')
        return observed


def recheck(backend, saved, live=True):
    backend.unchanged(saved['baseline'])
    a.need(inventory(a.NEW) == saved['candidate'], 'candidate-tree-drift')
    a.need(a.sha(RUN / 'rollback-dropin.private') == saved['baseline']['drop']['sha256'], 'rollback-dropin-copy-drift')
    if live:
        check_drop(saved['baseline']['drop'])
        a.need(backend.world(a.OLD, a.OLD_SHA) == saved['baseline']['world'], 'current-process-drift')
    backend.writers()


def stage(backend, package):
    a.validate_package(package)
    p = pins()
    # Verify assets before creating any candidate or private receipt.
    for name, pin in p['overlays'].items():
        path = HERE / 'assets' / name
        a.regular(path)
        a.need(path.stat().st_size == pin['bytes'] and a.sha(path) == pin['sha256'], 'overlay-source-drift')
    before = backend.original()
    side = p['generated_startup_sidecar']; side_path = a.OLD / side['path']
    side_stat = a.regular(side_path)
    a.need(side_stat.st_size == side['bytes'] and stat.S_IMODE(side_stat.st_mode) == int(side['mode'],8)
           and a.sha(side_path) == side['sha256'], 'known-startup-sidecar-drift')
    for name, pin in p['original_python_members'].items():
        a.regular(a.OLD / name)
        a.need(a.sha(a.OLD / name) == pin['sha256'], 'current-python-package-drift')
    for name, row in before['current'].items():
        a.need('link' not in row or name.startswith('population/') and len(Path(name).parts) == 2
               and Path(name).name not in ('python', 'policy'), 'unexpected-current-code-link')
    a.need(not a.NEW.exists() and not a.NEW.is_symlink(), 'successor-release-exists')
    a.need(not any(path != package for path in RUN.iterdir()), 'successor-run-not-empty')
    original_drop = a.DROP.read_bytes()
    a.need(digest(original_drop) == before['drop']['sha256'], 'dropin-snapshot-drift')
    a.write(RUN / 'rollback-dropin.private', original_drop)
    a.write(RUN / 'stage-owner.private.json', a.canonical({'performer': a.sha(Path(__file__)), 'pins': PINS_SHA,
            'old': str(a.OLD), 'new': str(a.NEW), 'baseline': before}))
    # Copy current code/config, preserving external state pointers rather than copying or rewinding earned data.
    shutil.copytree(a.OLD, a.NEW, symlinks=True,
                    ignore=lambda path, names: [n for n in names if n in ('logs', '__pycache__') or n.endswith('.pyc')])
    for name, row in inventory(a.NEW).items():
        a.need('link' not in row or name.startswith('population/') and len(Path(name).parts) == 2
               and Path(name).name not in ('python', 'policy'), 'unexpected-current-code-link')
    a.validate_package(package)
    with tarfile.open(package, 'r:gz') as archive:
        for member in archive:
            target = a.NEW / member.name
            raw = archive.extractfile(member).read()
            a.need(digest(raw) == p['members'][member.name]['sha256'], 'package-projection-drift')
            a.write(target, raw, member.mode, replace=target.exists())
    scripts = a.NEW / 'population/python/witness_scripts'
    a.need(not scripts.exists(), 'witness-scripts-already-exist')
    for name, pin in p['overlays'].items():
        target = a.NEW / name
        target.parent.mkdir(parents=True, exist_ok=True)
        a.write(target, (HERE / 'assets' / name).read_bytes(), int(pin['mode'], 8), replace=target.exists())
    world = a.NEW / 'etc/worldserver.conf'
    a.write(world, a.config_set(world.read_text(), {'LogsDir': '"' + str(a.NEW / 'logs') + '"',
            'Updates.EnableDatabases': '0'}).encode(), replace=True)
    conf = a.NEW / 'etc/modules/mod_python_engine.conf'
    # Stage is passive; bind deliberately enables the authorized selected-policy refresh.
    a.write(conf, a.config_set(conf.read_text(), {'Python.Enabled': '1', 'Python.Population.Enabled': '0',
            'Python.ScriptsPath': '"' + str(scripts) + '"'}).encode(), replace=True)
    (a.NEW / 'logs').mkdir(exist_ok=True)
    props = before['world']['props']; user = pwd.getpwnam(props['User']) if props['User'] else pwd.getpwuid(0)
    gid = grp.getgrnam(props['Group']).gr_gid if props['Group'] else user.pw_gid
    for root, dirs, files in os.walk(a.NEW, followlinks=False):
        os.chown(root, user.pw_uid, gid)
        for name in dirs + files:
            target = Path(root) / name
            if not target.is_symlink(): os.chown(target, user.pw_uid, gid)
    os.chmod(a.NEW, 0o750)
    for name, pin in p['overlays'].items():
        a.need(a.sha(a.NEW / name) == pin['sha256'], 'staged-overlay-drift')
    a.need({path.name for path in scripts.iterdir()} == {'native_witness.py', 'engine.py'}, 'startup-script-set')
    a.need(a.sha(a.NEW / side['path']) == side['sha256'], 'staged-startup-sidecar-drift')
    saved = {'performer': a.sha(Path(__file__)), 'pins': PINS_SHA, 'baseline': before,
             'candidate': inventory(a.NEW), 'config': None, 'preflight': None}
    recheck(backend, saved)
    a.write(RUN / 'staged.private.json', a.canonical(saved))
    return {'staged': True, 'configuration_bound': False, 'cutover_performed': False}


def saved_stage(backend):
    a.regular(RUN / 'staged.private.json')
    saved = json.loads((RUN / 'staged.private.json').read_bytes())
    a.need(saved['performer'] == a.sha(Path(__file__)) and saved['pins'] == PINS_SHA, 'successor-performer-drift')
    recheck(backend, saved, live=False)
    return saved


def roster_binding():
    accepted = pins()['accepted_roster']
    policy_path = a.NEW / 'population/policy/runtime-config.json'
    a.regular(policy_path)
    a.need(a.sha(policy_path) == accepted['policy_sha256'], 'accepted-policy-drift')
    policy = json.loads(policy_path.read_bytes())
    a.need(policy['online_cap'] == policy['durable_target'] == accepted['native_online_limit'] == 2500,
           'accepted-roster-limit-drift')
    manifest = Path(policy['manifest_path']); a.regular(manifest)
    a.need(a.sha(manifest) == accepted['manifest_sha256'] == policy['manifest_file_sha256'], 'accepted-manifest-drift')
    return dict(accepted, manifest_path=str(manifest))


def bind(backend, source, expected):
    saved = saved_stage(backend)
    recheck(backend, saved)
    a.need(saved['config'] is None, 'configuration-already-bound')
    a.regular(source); raw = source.read_bytes()
    a.need(digest(raw) == expected and len(raw) <= 65536, 'witness-config-hash-or-size')
    def pairs(rows):
        result = {}
        for key, value in rows:
            a.need(key not in result, 'duplicate-witness-config-key'); result[key] = value
        return result
    config = json.loads(raw, object_pairs_hook=pairs)
    fields = {'schema','identity_key','account_id','character_guid','account_type','effective_ceiling',
              'scheduler_eligible','phase','strategic_generation','snapshot_id','exporter_revision','seed_catalogue','dependencies'}
    a.need(set(config) == fields and config['schema'] == 'cpi225-selected-native-witness-v1', 'witness-config-schema')
    a.need(type(config['account_id']) is int and 0 < config['account_id'] < 2**32 and config['account_id'] != 142
           and type(config['character_guid']) is int and 0 < config['character_guid'] < 2**32
           and type(config['account_type']) is int and config['account_type'] in (1,2), 'witness-actor-binding')
    for key in ('identity_key','exporter_revision'):
        a.need(isinstance(config[key], str) and a.re.fullmatch('[0-9a-f]{64}', config[key]), 'witness-input-identity')
    a.need(type(config['effective_ceiling']) is int and 0 < config['effective_ceiling'] <= 80
           and type(config['scheduler_eligible']) is bool and type(config['strategic_generation']) is int
           and 0 < config['strategic_generation'] < 2**64 and isinstance(config['snapshot_id'],str)
           and config['snapshot_id'], 'witness-policy-binding')
    names = {'quest_policy','gear_policy','native_seed_policy','native_custody_ack','capture_native_acquisition','ingest_capture','witness_runtime'}
    expected_deps = {n+'.py': a.sha(a.NEW / 'population/python/lib' / (n+'.py')) for n in names}
    a.need(config['dependencies'] == expected_deps, 'witness-seven-dependencies')
    phase = config['phase']
    a.need(isinstance(phase, dict) and set(phase) == {'enabled','policy_valid','phase_id','level_ceiling','policy_digest'}
           and phase['enabled'] is True and phase['policy_valid'] is True and isinstance(phase['phase_id'],str)
           and phase['phase_id'] and type(phase['level_ceiling']) is int
           and config['effective_ceiling'] <= phase['level_ceiling'] <= 80
           and isinstance(phase['policy_digest'],str) and a.re.fullmatch('[0-9A-Fa-f]{64}',phase['policy_digest']), 'witness-phase-binding')
    descriptor = config['seed_catalogue']
    a.need(set(descriptor) == {'path','sha256'} and Path(descriptor['path']).is_absolute(), 'seed-original-descriptor')
    a.regular(Path(descriptor['path']))
    a.need(a.sha(descriptor['path']) == descriptor['sha256'], 'seed-original-drift')
    seed = json.loads(Path(descriptor['path']).read_bytes())
    a.need(seed['phase'] == config['phase'] and seed['observation_generation'] == config['strategic_generation'], 'seed-phase-generation-drift')
    roster = roster_binding()
    target = a.NEW / 'witness-config.private.json'
    a.write(target, raw, 0o600)
    os.chown(target, (a.NEW / 'etc/worldserver.conf').stat().st_uid, (a.NEW / 'etc/worldserver.conf').stat().st_gid)
    conf = a.NEW / 'etc/modules/mod_python_engine.conf'
    conf_stat = a.regular(conf)
    a.write(conf, a.config_set(conf.read_text(), {'Python.Population.Enabled': '1',
            'Python.Population.NativeOnlineLimit': '2500', 'Python.Population.EntryScript': '"native_witness.py"'}).encode(),
            stat.S_IMODE(conf_stat.st_mode), replace=True)
    os.chown(conf,conf_stat.st_uid,conf_stat.st_gid)
    saved.update(config={'sha256': expected, 'generation': config['strategic_generation'], 'dependencies': expected_deps,
                         'seed_original': descriptor, 'roster': roster, 'selected_policy_refresh_enabled': True, 'successor_loaded_acceptance': False}, candidate=inventory(a.NEW), preflight=None)
    recheck(backend, saved)
    a.write(RUN / 'staged.private.json', a.canonical(saved), replace=True)
    return {'configuration_bound': True, 'successor_loaded_acceptance': False}


def preflight(backend):
    saved = saved_stage(backend); recheck(backend, saved)
    observation = backend.executable_preflight(saved['baseline'])
    recheck(backend, saved)
    saved['preflight'] = observation
    a.write(RUN / 'staged.private.json', a.canonical(saved), replace=True)
    return {'preflight': observation, 'cutover_performed': False}


def restore_drop(saved, desired):
    expected = dict(saved['baseline']['drop'], sha256=digest(desired))
    check_drop(expected)
    a.need(a.DROP.read_bytes() == desired, 'recovery-dropin-not-owned')
    a.write(a.DROP, (RUN / 'rollback-dropin.private').read_bytes(), saved['baseline']['drop']['mode'], replace=True)
    os.chown(a.DROP, saved['baseline']['drop']['uid'], saved['baseline']['drop']['gid'])
    check_drop(saved['baseline']['drop'])
    a.command(['systemctl', 'daemon-reload'])


def recover(backend, saved, owned):
    recheck(backend, saved, live=False)
    desired = successor_drop(saved['baseline'], saved['config']['sha256'])
    check_drop(dict(saved['baseline']['drop'], sha256=digest(desired)))
    current = backend.world(a.NEW, a.NEW_SHA)
    if owned is None:
        a.need(current['process'] is None and current['props']['MainPID'] == '0'
               and current['props']['ActiveState'] in ('inactive','failed'), 'unknown-start-owner')
        a.need(current['props']['InvocationID'] == saved['baseline']['world']['props']['InvocationID'],
               'unowned-start-invocation')
    else:
        a.need(a.same_owner(current, owned) or a.stopped_owner(current, owned, backend.exit_identity()), 'rollback-process-not-owned')
    backend.writers(); backend.idle(active=current['process'] is not None)
    if current['process'] is not None: backend.stop()
    backend.unchanged(saved['baseline']); backend.writers(); backend.idle(active=False)
    restore_drop(saved, desired)
    restored = backend.start(a.OLD, a.OLD_SHA, current['props']['InvocationID'])
    return backend.ready(restored, a.OLD, a.OLD_SHA, saved['baseline'])


def cutover(backend):
    saved = saved_stage(backend)
    a.need(saved['config'] is not None and saved['preflight'] is not None, 'bind-and-preflight-required')
    recheck(backend, saved)
    descriptor = saved['config']['seed_original']; a.regular(descriptor['path'])
    a.need(a.sha(descriptor['path']) == descriptor['sha256'], 'seed-original-drift')
    a.need(not (RUN / 'cutover.private.json').exists(), 'cutover-already-attempted')
    a.need(roster_binding() == saved['config']['roster'], 'accepted-roster-binding-drift')
    desired = successor_drop(saved['baseline'], saved['config']['sha256'])
    a.write(RUN / 'cutover.private.json', a.canonical({'before': saved['baseline']['world'], 'start_requested': False}))
    result = {'activated': False, 'recovered': False, 'first_failure': None, 'recovery_failure': None,
              'successor_loaded_acceptance': False, 'gameplay_verified': False}
    stopped = replaced = False; owned = None
    try:
        # Final process/birth/invocation/deps/idle/writers checks happen under the caller's backup lock.
        backend.executable_preflight(saved['baseline'])
        recheck(backend, saved); backend.idle()
        a.need(roster_binding() == saved['config']['roster'], 'accepted-roster-binding-drift')
        stopped = True; backend.stop()
        backend.unchanged(saved['baseline']); backend.writers(); backend.idle(active=False)
        check_drop(saved['baseline']['drop'])
        a.write(a.DROP, desired, saved['baseline']['drop']['mode'], replace=True); replaced = True
        os.chown(a.DROP, saved['baseline']['drop']['uid'], saved['baseline']['drop']['gid'])
        a.command(['systemctl', 'daemon-reload'])
        owned = backend.start(a.NEW, a.NEW_SHA, saved['baseline']['world']['props']['InvocationID'])
        a.write(RUN / 'candidate-custody.private.json', a.canonical(owned))
        runtime = backend.ready(owned, a.NEW, a.NEW_SHA, saved['baseline'])
        observation = backend.python_observation(owned)
        a.need(observation['config_sha'] == saved['config']['sha256']
               and observation['config_generation'] == saved['config']['generation']
               and observation['dependencies'] == {k.removesuffix('.py'):v for k,v in saved['config']['dependencies'].items()}
               and observation['root'] == str(a.NEW / 'population/python/witness_scripts'), 'actual-python-binding-drift')
        result.update(activated=True, runtime=runtime, python_observation=observation, population_enabled=True,
                      native_online_limit=2500, selected_policy_refresh_enabled=True,
                      witness_tick_observed=('earned' in observation['status'] or bool(observation['status'].get('reason'))
                          and observation['status']['reason'] != 'awaiting_explicit_command'),
                      selected_policy_refresh_observed='earned' in observation['status'])
    except Exception as error:
        result['first_failure'] = a.safe_error(error)
        try:
            owned = owned or getattr(backend, 'start_owned', None)
            if replaced:
                result['recovery'] = recover(backend, saved, owned); result['recovered'] = True
            elif stopped:
                current = backend.world(a.OLD, a.OLD_SHA)
                a.need(current['process'] is None and current['props']['InvocationID'] == saved['baseline']['world']['props']['InvocationID'], 'unknown-stop-owner')
                check_drop(saved['baseline']['drop']); backend.unchanged(saved['baseline']); backend.writers(); backend.idle(active=False)
                restored = backend.start(a.OLD, a.OLD_SHA, current['props']['InvocationID'])
                result['recovery'] = backend.ready(restored,a.OLD,a.OLD_SHA,saved['baseline']); result['recovered'] = True
        except Exception as error:
            result['recovery_failure'] = a.safe_error(error)
    a.write(RUN / 'cutover-result.private.json', a.canonical(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('validate','stage','bind','preflight','cutover','rollback'))
    parser.add_argument('--package', type=Path, default=RUN / 'successor-world.tar.gz')
    parser.add_argument('--config', type=Path); parser.add_argument('--config-sha256')
    args = parser.parse_args()
    try:
        if args.mode == 'validate':
            result = {'package': a.validate_package(args.package), 'writes': False}
        else:
            a.need(os.geteuid() == 0 and RUN.is_dir() and RUN.resolve() == RUN and RUN.stat().st_mode & 0o077 == 0, 'root-private-successor-run-required')
            backend = Linux()
            with backend.lock():
                if args.mode == 'stage': result = stage(backend,args.package)
                elif args.mode == 'bind':
                    a.need(args.config is not None and args.config_sha256 is not None, 'physical-config-required')
                    result = bind(backend,args.config,args.config_sha256)
                elif args.mode == 'preflight': result = preflight(backend)
                elif args.mode == 'cutover': result = cutover(backend)
                else:
                    saved = saved_stage(backend); a.regular(RUN / 'candidate-custody.private.json')
                    result = {'recovered': True, 'recovery': recover(backend,saved,json.loads((RUN / 'candidate-custody.private.json').read_bytes()))}
                    a.write(RUN / 'rollback-result.private.json',a.canonical(result))
        print(json.dumps(result,sort_keys=True))
        return 1 if result.get('first_failure') else 0
    except Exception as error:
        print(json.dumps({'error':a.safe_error(error)},sort_keys=True)); return 1

if __name__ == '__main__':
    raise SystemExit(main())
