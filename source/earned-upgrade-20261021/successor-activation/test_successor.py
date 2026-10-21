"""Local fixtures only: no realm, SOAP, service, SQL or client operations."""
import copy
import io
import json
import os
from pathlib import Path
import pwd
import shlex
import select
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import successor as s

a = s.a

class Fake:
    def __init__(self, before):
        self.before = before
        self.current = copy.deepcopy(before['world'])
        self.events = []
        self.refuse_start = False
        self.drift = False

    def original(self): return copy.deepcopy(self.before)
    def unchanged(self, before):
        a.need(s.inventory(a.OLD) == before['current'], 'rollback-release-drift')
    def world(self, prefix, expected):
        value = copy.deepcopy(self.current)
        if self.drift: value['process']['startticks'] += 1
        return value
    def writers(self): self.events.append('writers')
    def idle(self, active=True): self.events.append('idle')
    def stop(self):
        self.events.append('stop')
        self.current['props'].update(MainPID='0',ActiveState='inactive',SubState='dead')
        self.current['process'] = None
    def start(self, prefix, expected, previous):
        self.events.append('start-'+prefix.name)
        if prefix == a.NEW and self.refuse_start:
            raise a.Refusal('refused-before-process-start')
        self.current = copy.deepcopy(self.before['world'])
        self.current['props'].update(InvocationID=prefix.name,MainPID='20')
        self.current['process'] = {'pid':20,'startticks':222,'sha256':expected}
        self.current['exit_custody'] = self.exit_identity()
        self.start_owned = copy.deepcopy(self.current)
        return copy.deepcopy(self.current)
    def props(self): return self.current['props']
    def exit_identity(self):
        return {'ExecMainPID':'20','ExecMainStartTimestampMonotonic':'222000','InvocationID':self.current['props']['InvocationID']}
    def ready(self, owned, prefix, expected, before):
        return {'world':owned,'python_ready':True,'gameplay_verified':False}
    def executable_preflight(self, baseline):
        self.events.append('preflight')
        return {'sha256':s.pins()['members']['bin/worldserver']['sha256']}
    def python_observation(self, owned):
        saved=json.loads((s.RUN/'staged.private.json').read_bytes())
        return {'config_sha':saved['config']['sha256'],'config_generation':saved['config']['generation'],
                'dependencies':{k.removesuffix('.py'):v for k,v in saved['config']['dependencies'].items()},
                'root':str(a.NEW/'population/python/witness_scripts'), 'mode':'observe','armed':False,'status':{'mode':'observe','reason':'awaiting_explicit_command'}}

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=s.HERE); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for owner,key,value in ((a,'OLD',self.root/'old'),(a,'NEW',self.root/'new'),(a,'BASE',self.root/'base'),
                                (a,'DROP',self.root/'90-owned.conf'),(s,'RUN',self.root/'run')):
            p=patch.object(owner,key,value);p.start();self.addCleanup(p.stop)
        s.RUN.mkdir(mode=0o700)
        for name in ('bin','etc/modules','python','population/python/lib','population/python/scripts','population/policy'):
            (a.OLD/name).mkdir(parents=True,exist_ok=True)
        (a.OLD/'bin/worldserver').write_bytes(b'old-world'); (a.OLD/'bin/worldserver').chmod(0o755)
        (a.OLD/'build-provenance.json').write_text('{}')
        (a.OLD/'bin/dc_test_dungeons.json').write_bytes(b'known-sidecar')
        (a.OLD/'python/engine.py').write_text('#old engine')
        (a.OLD/'population/python/scripts/population_bootstrap.py').write_text('raise RuntimeError("never discover")')
        (a.OLD/'etc/worldserver.conf').write_text('Updates.EnableDatabases = 1\n')
        (a.OLD/'etc/modules/mod_python_engine.conf').write_text('Python.Population.Enabled = 0\nPython.Population.NativeOnlineLimit = 0\n')
        (a.OLD/'population/policy/original.json').write_text('{"earned":true}')
        self.dependencies={}
        for name in ('quest_policy','gear_policy','native_seed_policy','native_custody_ack','capture_native_acquisition','ingest_capture'):
            path=a.OLD/'population/python/lib'/(name+'.py');path.write_text('#original '+name)
            self.dependencies['population/python/lib/'+name+'.py']={'sha256':a.sha(path),'bytes':path.stat().st_size,'mode':'0o644'}
        self.drop=(f'[Service]\nExecStart=\nExecStart={a.OLD}/bin/worldserver -c {a.OLD}/etc/worldserver.conf\n'
                   f'WorkingDirectory={a.OLD}/bin\nBindReadOnlyPaths={a.OLD}/etc/modules:/srv/wow/server/etc/modules\nRestart=no\n').encode()
        a.DROP.write_bytes(self.drop);a.DROP.chmod(0o640)
        self.data={'bin/worldserver':b'new-world','build-provenance.json':b'{}'}
        self.package=self.root/'candidate.tar.gz'
        with tarfile.open(self.package,'w:gz') as archive:
            for name,data in self.data.items():
                member=tarfile.TarInfo(name);member.size=len(data);member.mode=0o755 if name=='bin/worldserver' else 0o644
                archive.addfile(member,io.BytesIO(data))
        self.pins=s.pins(); self.pins['package']={'sha256':a.sha(self.package),'bytes':self.package.stat().st_size}
        self.pins['members']={name:{'sha256':s.digest(data),'bytes':len(data),'mode':'0o755' if name=='bin/worldserver' else '0o644'} for name,data in self.data.items()}
        self.pins['original_python_members']=self.dependencies
        manifest=self.root/'manifest.original.json';manifest.write_text('{"identities":"fixture authenticated only"}')
        policy=a.OLD/'population/policy/runtime-config.json'
        policy.write_text(json.dumps({'online_cap':2500,'durable_target':2500,'manifest_path':str(manifest),'manifest_file_sha256':a.sha(manifest)}))
        self.pins['accepted_roster']={'policy_sha256':a.sha(policy),'manifest_sha256':a.sha(manifest),'native_online_limit':2500}
        self.pins['generated_startup_sidecar']={'path':'bin/dc_test_dungeons.json','sha256':s.digest(b'known-sidecar'),'bytes':len(b'known-sidecar'),'mode':'0o644'}
        for owner,key,value in ((s,'pins',lambda:self.pins),(a,'OLD_SHA',s.digest(b'old-world')),(a,'NEW_SHA',s.digest(b'new-world'))):
            p=patch.object(owner,key,value);p.start();self.addCleanup(p.stop)
        props={name:'' for name in a.CONFIG_PROPS}
        props.update(User=pwd.getpwuid(os.getuid()).pw_name,Group='',MainPID='10',InvocationID='old',
                     NRestarts='0',ActiveState='active',SubState='running',BindReadOnlyPaths=shlex.quote(str(a.OLD/'etc/modules')+':/srv/wow/server/etc/modules'))
        self.before={'world':{'props':props,'process':{'pid':10,'startticks':100,'sha256':a.OLD_SHA}},
                     'current':s.inventory(a.OLD),'drop':s.drop_snapshot(),'deps':{},'service_files':{},'policy':{},'idle':{}}
        self.backend=Fake(self.before)
        p=patch.object(a,'command',lambda *args,**kwargs:'');p.start();self.addCleanup(p.stop)

    def stage(self): return s.stage(self.backend,self.package)
    def bind(self):
        self.stage()
        phase={'enabled':True,'policy_valid':True,'phase_id':'original','level_ceiling':80,'policy_digest':'A'*64}
        catalogue=self.root/'catalogue.original.json';catalogue.write_text(json.dumps({'phase':phase,'observation_generation':5}))
        config={'schema':'cpi225-selected-native-witness-v1','identity_key':'a'*64,'account_id':27,'character_guid':28,
                'account_type':1,'effective_ceiling':10,'scheduler_eligible':True,'phase':phase,'strategic_generation':5,
                'snapshot_id':'original','exporter_revision':'b'*64,'seed_catalogue':{'path':str(catalogue),'sha256':a.sha(catalogue)},
                'dependencies':{name+'.py':a.sha(a.NEW/'population/python/lib'/(name+'.py')) for name in
                ('quest_policy','gear_policy','native_seed_policy','native_custody_ack','capture_native_acquisition','ingest_capture','witness_runtime')}}
        path=self.root/'config.private.json';path.write_text(json.dumps(config))
        s.bind(self.backend,path,a.sha(path));s.preflight(self.backend)

    def test_stage_preserves_current_and_only_two_startup_scripts(self):
        original=s.inventory(a.OLD);self.stage()
        self.assertEqual(s.inventory(a.OLD),original)
        self.assertEqual((a.NEW/'bin/dc_test_dungeons.json').read_bytes(),b'known-sidecar')
        self.assertEqual(a.DROP.read_bytes(),self.drop)
        self.assertEqual((s.RUN/'rollback-dropin.private').read_bytes(),self.drop)
        scripts=a.NEW/'population/python/witness_scripts'
        self.assertEqual({p.name for p in scripts.iterdir()},{'native_witness.py','engine.py'})
        for name,pin in self.pins['overlays'].items(): self.assertEqual(a.sha(a.NEW/name),pin['sha256'])
        conf=(a.NEW/'etc/modules/mod_python_engine.conf').read_text()
        self.assertIn('Python.Population.Enabled = 0',conf)
        self.assertIn('Python.Population.NativeOnlineLimit = 0',conf)
        self.assertIn(str(scripts),conf)
        self.assertIn('Updates.EnableDatabases = 0',(a.NEW/'etc/worldserver.conf').read_text())

    def test_wrong_outer_package_writes_nothing(self):
        self.pins['package']['sha256']='0'*64
        with self.assertRaisesRegex(a.Refusal,'package-identity'):self.stage()
        self.assertFalse(a.NEW.exists());self.assertEqual(list(s.RUN.iterdir()),[])

    def test_wrong_member_writes_nothing(self):
        self.pins['members']['bin/worldserver']['sha256']='0'*64
        with self.assertRaisesRegex(a.Refusal,'archive-member-hash'):self.stage()
        self.assertFalse(a.NEW.exists())

    def test_extra_archive_member_refuses_without_stage(self):
        with tarfile.open(self.package,'w:gz') as archive:
            for name,data in dict(self.data,unexpected=b'extra').items():
                member=tarfile.TarInfo(name);member.size=len(data);member.mode=0o755 if name=='bin/worldserver' else 0o644
                archive.addfile(member,io.BytesIO(data))
        self.pins['package']={'sha256':a.sha(self.package),'bytes':self.package.stat().st_size}
        with self.assertRaisesRegex(a.Refusal,'archive-unlisted-member'):self.stage()
        self.assertFalse(a.NEW.exists())

    def test_service_user_preflight_checks_elf_ldd_and_supported_help(self):
        self.stage(); calls=[]
        def command(argv,**kwargs):
            calls.append(argv)
            if argv[0]=='readelf': return 'Class: ELF64\nMachine: Advanced Micro Devices X86-64'
            if 'ldd' in argv: return 'libc.so.6 => /lib64/libc.so.6'
            if '--help' in argv: return 'Options: --help --config'
            return ''
        with patch.object(a,'command',command): result=s.Linux().executable_preflight(self.before)
        self.assertEqual(result['help_argv'],[str(a.NEW/'bin/worldserver'),'--help'])
        self.assertTrue(any(row[0]=='setpriv' and '--help' in row for row in calls))
        self.assertTrue(any(row[0]=='setpriv' and 'ldd' in row for row in calls))
        self.assertFalse(any('--version' in row or 'systemctl' in row for row in calls))

    def test_nonexecutable_mode_refuses_before_help(self):
        self.stage(); (a.NEW/'bin/worldserver').chmod(0o644)
        with self.assertRaisesRegex(a.Refusal,'preflight-executable-drift'):s.Linux().executable_preflight(self.before)

    def test_missing_dynamic_dependency_refuses_before_help(self):
        self.stage();calls=[]
        def command(argv,**kwargs):
            calls.append(argv)
            if argv[0]=='readelf':return 'ELF64 X86-64'
            return 'libbad.so => not found' if 'ldd' in argv else ''
        with patch.object(a,'command',command):
            with self.assertRaisesRegex(a.Refusal,'preflight-missing-dependency'):s.Linux().executable_preflight(self.before)
        self.assertFalse(any('--help' in row for row in calls))

    def check_real_child_fd(self, relative, refused):
        self.stage()
        target = self.root / relative if relative else None
        child = subprocess.Popen([sys.executable, '-B', '-c',
            'import sys; f=open(sys.argv[1],"r+b") if sys.argv[1] else None; print("ready",flush=True); sys.stdin.read()',
            str(target) if target else ''], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'child FD readiness timed out')
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            original_iterdir = Path.iterdir
            def narrowed(path):
                return iter([Path('/proc') / str(child.pid)]) if path == Path('/proc') else original_iterdir(path)
            with patch.object(Path, 'iterdir', narrowed):
                if refused:
                    with self.assertRaisesRegex(a.Refusal, 'open-code-config-writer'): s.Linux().writers()
                else:
                    s.Linux().writers()
        finally:
            try:
                _, stderr = child.communicate('', timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.communicate(); raise
            self.assertEqual(child.returncode, 0, stderr)

    def test_real_writer_on_successor_world_refuses(self):
        self.check_real_child_fd('new/bin/worldserver', True)

    def test_real_writer_on_rollback_population_python_refuses(self):
        self.check_real_child_fd('old/population/python/lib/quest_policy.py', True)

    def test_real_writer_on_existing_protected_candidate_python_refuses(self):
        self.check_real_child_fd('new/population/python/lib/witness_runtime.py', True)

    def test_child_without_open_protected_writer_passes(self):
        self.check_real_child_fd(None, False)

    def test_binding_and_preflight_do_not_start(self):
        self.bind();self.assertNotIn('stop',self.backend.events)
        conf=(a.NEW/'etc/modules/mod_python_engine.conf').read_text()
        self.assertIn('Python.Population.Enabled = 1',conf)
        self.assertIn('Python.Population.NativeOnlineLimit = 2500',conf)
        self.assertIn('Python.Population.EntryScript = "native_witness.py"',conf)
        self.assertFalse(json.loads((s.RUN/'staged.private.json').read_bytes())['config']['successor_loaded_acceptance'])

    def test_wrong_accepted_policy_refuses_before_enabling(self):
        self.pins['accepted_roster']['policy_sha256']='0'*64
        with self.assertRaisesRegex(a.Refusal,'accepted-policy-drift'):self.bind()
        self.assertNotIn('stop',self.backend.events)
        self.assertIn('Python.Population.Enabled = 0',(a.NEW/'etc/modules/mod_python_engine.conf').read_text())

    def test_wrong_accepted_manifest_refuses_before_enabling(self):
        self.pins['accepted_roster']['manifest_sha256']='0'*64
        with self.assertRaisesRegex(a.Refusal,'accepted-manifest-drift'):self.bind()
        self.assertNotIn('stop',self.backend.events)
        self.assertIn('Python.Population.Enabled = 0',(a.NEW/'etc/modules/mod_python_engine.conf').read_text())

    def test_cutover_without_binding_refuses(self):
        self.stage()
        with self.assertRaisesRegex(a.Refusal,'bind-and-preflight-required'):s.cutover(self.backend)
        self.assertNotIn('stop',self.backend.events)

    def test_wrong_drop_refuses_before_stop(self):
        self.bind();a.DROP.write_bytes(b'unknown owner')
        with self.assertRaisesRegex(a.Refusal,'owned-dropin-drift'):s.cutover(self.backend)
        self.assertNotIn('stop',self.backend.events)

    def test_wrong_process_birth_refuses_before_stop(self):
        self.bind();self.backend.drift=True
        with self.assertRaisesRegex(a.Refusal,'current-process-drift'):s.cutover(self.backend)
        self.assertNotIn('stop',self.backend.events)

    def test_refused_before_process_start_restores_exact_current_drop(self):
        self.bind();self.backend.refuse_start=True
        result=s.cutover(self.backend)
        self.assertFalse(result['activated']);self.assertTrue(result['recovered']);self.assertIsNone(result['recovery_failure'])
        self.assertEqual(a.DROP.read_bytes(),self.drop);self.assertEqual(a.DROP.stat().st_mode&0o777,0o640)
        self.assertIn('start-old',self.backend.events)
        self.assertFalse(result['successor_loaded_acceptance']);self.assertFalse(result['gameplay_verified'])

    def test_changed_failed_start_invocation_refuses_unknown_recovery(self):
        self.bind()
        def unknown(prefix,expected,previous):
            self.backend.current['props']['InvocationID']='foreign'
            raise a.Refusal('failed-unowned-start')
        self.backend.start=unknown
        result=s.cutover(self.backend)
        self.assertFalse(result['recovered']);self.assertEqual(result['recovery_failure']['code'],'unowned-start-invocation')
        self.assertNotEqual(a.DROP.read_bytes(),self.drop)

    def test_positive_observations_remain_unaccepted_and_rollback_keeps_database(self):
        self.bind();result=s.cutover(self.backend)
        self.assertTrue(result['activated']);self.assertFalse(result['gameplay_verified']);self.assertFalse(result['successor_loaded_acceptance'])
        self.assertTrue(result['population_enabled']);self.assertEqual(result['native_online_limit'],2500)
        self.assertFalse(result['witness_tick_observed'])
        self.assertIn(b'Restart=no',a.DROP.read_bytes())
        saved=s.saved_stage(self.backend);owned=json.loads((s.RUN/'candidate-custody.private.json').read_bytes())
        s.recover(self.backend,saved,owned)
        self.assertEqual(a.DROP.read_bytes(),self.drop)
        self.assertEqual((a.OLD/'population/policy/original.json').read_text(),'{"earned":true}')

    def test_overlay_drift_refuses_before_stop(self):
        self.bind();(a.NEW/'population/python/lib/witness_runtime.py').write_text('foreign')
        with self.assertRaisesRegex(a.Refusal,'candidate-tree-drift'):s.cutover(self.backend)
        self.assertNotIn('stop',self.backend.events)

class ActualSourceTests(unittest.TestCase):
    def test_actual_pins_and_reviewed_asset_hashes(self):
        value=s.pins()
        self.assertEqual(value['members']['bin/worldserver']['sha256'],a.NEW_SHA)
        self.assertEqual(len(value['members']),2)
        self.assertEqual(value['module']['sha256'],'53e5a03cf92b6f2dee85cf685efd67a59d10ea0582d06e18681ee6cd9b7e57f0')
        for name,pin in value['overlays'].items(): self.assertEqual(a.sha(s.HERE/'assets'/name),pin['sha256'])

if __name__=='__main__': unittest.main()
