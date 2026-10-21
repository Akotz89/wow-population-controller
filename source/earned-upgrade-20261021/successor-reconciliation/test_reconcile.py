"""Local specific-process fixtures and actual pinned transport source; no remote actions."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import os
import select
import subprocess
import types
import unittest
from unittest.mock import patch

import reconcile as r
sys.path.insert(0,str(r.HELPER.parent))
import test_successor as previous_tests

a = r.a
s = r.s

class Fake(previous_tests.Fake):
    def verify_bindings(self,prefix,current): pass
    def script_mounts(self,current): pass
    def startup_journal(self,current): return [{'MESSAGE':'fixture publication/source ready','_PID':str(current['process']['pid'])}]
    def namespace(self): return copy.deepcopy(self.observed_namespace)
    def soap(self,text):
        return ('Python Engine: Ready\nReloading: No\nConfigured Scripts Path: '+str(a.NEW/'population/python/witness_scripts')
                if text=='python status' else 'source sha256:'+r.pins()['source_identity']+'; Connected players: 0. Characters in world: 0.')
    def exit_identity(self): return self.epoch
    def idle(self,active=True): return {'characters':0,'accounts':0,'sockets':0,'soap_connected':0}

class ReconcileTests(unittest.TestCase):
    def setUp(self):
        original_temp = tempfile.TemporaryDirectory
        def owned_temp(*args,**kwargs):
            kwargs['dir'] = r.HERE
            return original_temp(*args,**kwargs)
        with patch.object(tempfile,'TemporaryDirectory',owned_temp):
            self.fixture = previous_tests.Fixture(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.bind()
        saved = json.loads((s.RUN/'staged.private.json').read_bytes())
        p = copy.deepcopy(r.pins())
        p['process'] = {'pid':1705346,'startticks':84525821,'sha256':a.NEW_SHA,
                        'exe':str(a.NEW/'bin/worldserver'),'cwd':str(a.NEW/'bin'),
                        'argv':[str(a.NEW/'bin/worldserver'),'-c',str(a.NEW/'etc/worldserver.conf')]}
        p['config_sha256'] = saved['config']['sha256'];p['config_generation'] = saved['config']['generation']
        p['dependencies'] = {k.removesuffix('.py'):v for k,v in saved['config']['dependencies'].items()}
        self.pins = p
        patcher = patch.object(r,'pins',lambda:self.pins);patcher.start();self.addCleanup(patcher.stop)
        a.write(a.DROP,s.successor_drop(saved['baseline'],p['config_sha256']),saved['baseline']['drop']['mode'],replace=True)
        props = copy.deepcopy(saved['baseline']['world']['props'])
        for key in ('WorkingDirectory','BindReadOnlyPaths'):props[key]=props[key].replace(str(a.OLD),str(a.NEW))
        props['WorkingDirectory']=str(a.NEW/'bin')
        # The historical fixture leaves WorkingDirectory empty; the actual baseline contains OLD/bin.
        saved['baseline']['world']['props']['WorkingDirectory']=str(a.OLD/'bin')
        self.fixture.baseline = saved['baseline']
        props.update(MainPID=str(p['process']['pid']),InvocationID=p['invocation'],ActiveState='active',SubState='running',NRestarts='0',
            ExecStart='{ path='+str(a.NEW/'bin/worldserver')+' ; argv[]='+str(a.NEW/'bin/worldserver')+' -c '+str(a.NEW/'etc/worldserver.conf')+' ; }')
        self.backend = Fake(saved['baseline'])
        self.backend.current={'props':props,'process':copy.deepcopy(p['process'])}
        self.backend.epoch={'ExecMainPID':str(p['process']['pid']),'InvocationID':p['invocation'],'ExecMainStartTimestampMonotonic':'845258200000'}
        self.backend.observed_namespace=json.loads((r.HERE.parent/'evidence/successor-namespace-readback.json').read_bytes())
        self.backend.observed_namespace.update(root=str(a.NEW/'population/python/witness_scripts'),config_sha=p['config_sha256'],
            config_generation=p['config_generation'],dependencies=p['dependencies'])
        owner={'performer':p['helper_sha256'],'pins':p['helper_pins_sha256'],'old':str(a.OLD),'new':str(a.NEW),'baseline':saved['baseline']}
        for name,value in (('staged.private.json',saved),('stage-owner.private.json',owner),
            ('cutover.private.json',{'before':saved['baseline']['world'],'start_requested':False}),('cutover-result.private.json',p['failed_result'])):
            a.write(s.RUN/name,a.canonical(value),replace=(s.RUN/name).exists())
        self.originals={name:(s.RUN/name).read_bytes() for name in ('staged.private.json','stage-owner.private.json','cutover.private.json','cutover-result.private.json','rollback-dropin.private')}

    def check_no_custody(self): self.assertFalse((s.RUN/'candidate-custody.private.json').exists())

    def test_specific_process_adopts_idempotently_without_changing_failure(self):
        first=r.reconcile(self.backend);second=r.reconcile(self.backend)
        self.assertEqual(first,second);self.assertTrue(first['reconciled']);self.assertFalse(first['original_cutover_activated'])
        current=json.loads((s.RUN/'candidate-custody.private.json').read_bytes())
        self.assertEqual(current['exit_custody'],self.backend.epoch)
        for name,raw in self.originals.items():self.assertEqual((s.RUN/name).read_bytes(),raw)
        self.assertNotIn('stop',self.backend.events)

    def with_real_log_fd(self, check, missing_publication=False):
        log=self.pins['startup_log']
        log.update(source_header='AzerothCore source '+self.pins['source_identity']+' (worldserver-daemon)',
            publication=">> Published 2 Python scripts from canonical root '"+str(a.NEW/'population/python/witness_scripts')+"' in 2419 ms.",
            ready='AzerothCore source '+self.pins['source_identity']+' (worldserver-daemon) ready...')
        path=a.NEW/'logs/Server.log'
        raw=('start\n'+log['source_header']+'\n'+('' if missing_publication else log['publication']+'\n')+log['ready']+'\n').encode()
        path.write_bytes(raw);st=path.stat();log.update(device=st.st_dev,inode=st.st_ino,prefix_bytes=len(raw))
        child=subprocess.Popen([sys.executable,'-B','-c',
            'import sys; f=open(sys.argv[1],"r+b"); print(f.fileno(),flush=True); sys.stdin.read()',str(path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertTrue(select.select([child.stdout],[],[],5)[0]);log['fd']=child.stdout.readline().strip()
            process=a.Linux.process(self.backend,child.pid)
            current={'props':dict(self.backend.current['props'],MainPID=str(child.pid)),
                'process':process,'exit_custody':dict(self.backend.epoch,ExecMainPID=str(child.pid))}
            reader=r.Linux()
            original_iterdir=Path.iterdir
            def observed_processes(directory):
                if directory == Path('/proc'):return iter([Path('/proc')/str(child.pid),Path('/proc')/str(os.getpid())])
                return original_iterdir(directory)
            with patch.object(reader,'world',return_value=current),patch.object(reader,'exit_identity',return_value=current['exit_custody']), \
                 patch.object(Path,'iterdir',observed_processes):
                check(reader,current,path)
        finally:
            try: _,stderr=child.communicate('',timeout=5)
            except subprocess.TimeoutExpired: child.kill();child.communicate();raise
            self.assertEqual(child.returncode,0,stderr)

    def denied_foreign_fd(self, flags, same_inode, fdinfo_denied=False):
        def check(reader,current,path):
            foreign=Path('/proc/768621');descriptor=foreign/'fd/255';metadata=foreign/'fdinfo/255'
            original_iterdir=Path.iterdir;original_read=Path.read_text;original_stat=Path.stat
            stat_calls=[]
            def entries(directory):
                if directory == Path('/proc'):return iter(list(original_iterdir(directory))+[foreign])
                if directory == foreign/'fd':return iter([descriptor])
                return original_iterdir(directory)
            def read(target,*args,**kwargs):
                if target == metadata:
                    if fdinfo_denied:raise PermissionError('fixture unreadable fdinfo')
                    inode=path.stat().st_ino+(0 if same_inode else 1)
                    return 'pos: 0\nflags: '+flags+'\nmnt_id: 123\nino: '+str(inode)+'\n'
                return original_read(target,*args,**kwargs)
            def stat(target,*args,**kwargs):
                if target == descriptor:
                    stat_calls.append(target);raise PermissionError('fixture denied container descriptor stat')
                return original_stat(target,*args,**kwargs)
            with patch.object(Path,'iterdir',entries),patch.object(Path,'read_text',read),patch.object(Path,'stat',stat):
                if fdinfo_denied or same_inode and int(flags,8)&3:
                    with self.assertRaises(PermissionError):reader.owned_log_prefix(current)
                else:
                    self.assertEqual(reader.owned_log_prefix(current)['inode'],path.stat().st_ino)
            self.assertEqual(len(stat_calls),1 if same_inode and int(flags,8)&3 and not fdinfo_denied else 0)
        self.with_real_log_fd(check)

    def test_readonly_fd_is_skipped_before_denied_stat(self):
        self.denied_foreign_fd('02100000',True)

    def test_different_inode_fd_is_skipped_before_denied_stat(self):
        self.denied_foreign_fd('0100002',False)

    def test_matching_writable_inode_denied_stat_still_refuses(self):
        self.denied_foreign_fd('0100002',True)

    def test_unreadable_fdinfo_still_refuses(self):
        self.denied_foreign_fd('02100000',False,fdinfo_denied=True)

    def test_real_owned_log_fd_matches_and_allows_ordinary_append(self):
        def check(reader,current,path):
            first=reader.owned_log_prefix(current)
            with path.open('ab') as out:out.write(b'ordinary later log line\n')
            self.assertEqual(reader.owned_log_prefix(current,first),first)
            self.assertEqual(first['inode'],path.stat().st_ino)
            self.assertEqual(set(first['records']),{'source_header','publication','ready'})
        self.with_real_log_fd(check)

    def test_real_owned_log_missing_publication_refuses(self):
        def check(reader,current,path):
            with self.assertRaisesRegex(a.Refusal,'owned-startup-log-records-absent'):reader.owned_log_prefix(current)
        self.with_real_log_fd(check,missing_publication=True)

    def test_real_owned_log_foreign_inode_refuses(self):
        def check(reader,current,path):
            path.rename(path.with_suffix('.old'));path.write_bytes(path.with_suffix('.old').read_bytes())
            with self.assertRaisesRegex(a.Refusal,'owned-startup-log-identity-drift'):reader.owned_log_prefix(current)
        self.with_real_log_fd(check)

    def test_real_owned_log_prefix_rewrite_refuses(self):
        def check(reader,current,path):
            first=reader.owned_log_prefix(current)
            data=path.read_bytes();path.write_bytes(b'X'+data[1:])
            with self.assertRaisesRegex(a.Refusal,'owned-startup-log-prefix-or-owner-rewritten'):reader.owned_log_prefix(current,first)
        self.with_real_log_fd(check)

    def test_real_owned_log_unknown_second_writer_refuses(self):
        def check(reader,current,path):
            with path.open('r+b'):
                with self.assertRaisesRegex(a.Refusal,'owned-startup-log-foreign-writer'):reader.owned_log_prefix(current)
        self.with_real_log_fd(check)

    def test_absent_journal_uses_owned_log_without_fabricated_journal_rows(self):
        self.backend.startup_journal=lambda current: (_ for _ in ()).throw(a.Refusal('startup-publication-journal-absent'))
        proof={'path':str(a.NEW/'logs/Server.log'),'prefix_bytes':99,'prefix_sha256':'fixture-log-proof'}
        checks=[]
        def read(current,expected=None):
            checks.append(expected)
            if expected is not None:self.assertEqual(expected,proof)
            return proof
        self.backend.owned_log_prefix=read
        self.assertTrue(r.reconcile(self.backend)['reconciled'])
        observed=json.loads((s.RUN/'post-failure-observation.private.json').read_bytes())
        self.assertEqual(observed['startup_evidence']['kind'],'owned-server-log-prefix')
        self.assertTrue(observed['startup_evidence']['journal_publication_pair_absent'])
        self.assertNotIn('journal',observed['startup_evidence'])
        self.assertGreaterEqual(len(checks),5)

    def test_wrong_pid_refuses(self):
        self.backend.current['process']['pid']+=1
        with self.assertRaisesRegex(a.Refusal,'specific-process-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_wrong_birth_refuses(self):
        self.backend.current['process']['startticks']+=1
        with self.assertRaisesRegex(a.Refusal,'specific-process-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_wrong_invocation_refuses(self):
        self.backend.current['props']['InvocationID']='unknown'
        with self.assertRaisesRegex(a.Refusal,'specific-unit-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_wrong_drop_refuses(self):
        a.DROP.write_bytes(b'foreign')
        with self.assertRaisesRegex(a.Refusal,'owned-dropin-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_wrong_namespace_refuses(self):
        self.backend.observed_namespace['config_generation']+=1
        with self.assertRaisesRegex(a.Refusal,'specific-namespace-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_pending_capture_refuses(self):
        self.backend.observed_namespace['status']['capture']={'state':'armed'}
        with self.assertRaisesRegex(a.Refusal,'specific-witness-state-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_wrong_failed_cutover_lineage_refuses(self):
        (s.RUN/'cutover-result.private.json').write_text('{}')
        with self.assertRaisesRegex(a.Refusal,'failed-cutover-lineage-drift'):r.reconcile(self.backend)
        self.check_no_custody()

    def test_existing_unknown_custody_is_not_overwritten(self):
        (s.RUN/'candidate-custody.private.json').write_text('{}')
        with self.assertRaisesRegex(a.Refusal,'existing-reconciliation-output-drift'):r.reconcile(self.backend)
        self.assertEqual((s.RUN/'candidate-custody.private.json').read_text(),'{}')

class ActualTransportTests(unittest.TestCase):
    def test_real_namespace_reader_executes_only_chunked_status_expressions(self):
        library=r.HERE.parent/'../core2-current-native-world-package-20261021/attempts/06/run/artifacts/view/task/build-design/output/delivery/stage/population/python/lib'
        frames,parse=r.transport(library.resolve())
        expected=json.loads((r.HERE.parent/'evidence/successor-namespace-readback.json').read_bytes())
        witness=types.SimpleNamespace(config={'strategic_generation':expected['config_generation']},mode='observe',
            strategy=types.SimpleNamespace(acquisition_capture=None))
        scope={'__script_root__':expected['root'],'ENGINE_READY':True,'ENGINE_STARTUPS':1,'_config_sha':expected['config_sha'],
            '_witness':witness,'population_tick':lambda diff:True,'_names':set(expected['dependencies']),
            'witness_status':lambda:copy.deepcopy(expected['status'])}
        modules={}
        for name,digest in expected['dependencies'].items():
            module=types.ModuleType(name);module._witness_source_sha256=digest;modules[name]=module
        modules['capture_native_acquisition'].NativeCaptureFrames=frames
        commands=[]
        def soap(command,*args,**kwargs):
            commands.append(command);self.assertTrue(command.startswith('python exec '))
            output=io.StringIO()
            with patch('sys.stdout',output):exec(command.removeprefix('python exec '),scope,scope)
            lines=output.getvalue().splitlines()
            self.assertTrue(all(len(line)<=240 for line in lines));self.assertLessEqual(len(lines),10)
            return {'ok':True,'response':'Output:\r\n'+'\r\n'.join(lines)}
        with patch.object(r,'transport',return_value=(frames,parse)),patch.object(a,'read_credentials',return_value={}), \
             patch.object(a,'soap_command',soap),patch.dict(sys.modules,modules):
            observed=r.Linux().namespace()
        self.assertEqual(observed,expected);self.assertEqual(len(commands),3)
        self.assertTrue(all('NativeCaptureFrames.emit(' in command for command in commands))

    def test_actual_journal_reader_requires_same_invocation_publication_and_ready_source(self):
        p=r.pins();current={'props':{'InvocationID':p['invocation']},'process':p['process'],
            'exit_custody':{'ExecMainStartTimestampMonotonic':'10'}}
        common={'_PID':str(p['process']['pid']),'_SYSTEMD_INVOCATION_ID':p['invocation'],
            '__MONOTONIC_TIMESTAMP':'20','__REALTIME_TIMESTAMP':'100'}
        rows=[dict(common,MESSAGE=">> Published 2 Python scripts from canonical root '"+str(a.NEW/'population/python/witness_scripts')+"' in 2419 ms."),
              dict(common,MESSAGE='AzerothCore source sha256:'+p['source_identity']+'; (worldserver-daemon) ready...')]
        with patch.object(a,'command',return_value='\n'.join(json.dumps(row) for row in rows)):
            self.assertEqual(len(r.Linux().startup_journal(current)),2)
        rows[0]['_SYSTEMD_INVOCATION_ID']='foreign'
        with patch.object(a,'command',return_value='\n'.join(json.dumps(row) for row in rows)):
            with self.assertRaisesRegex(a.Refusal,'startup-journal-owner-drift'):r.Linux().startup_journal(current)

    def test_existing_chunked_emitter_parser_survives_250_character_lines(self):
        library=r.HERE.parent/'../core2-current-native-world-package-20261021/attempts/06/run/artifacts/view/task/build-design/output/delivery/stage/population/python/lib'
        frames,parse=r.transport(library.resolve())
        value=json.loads((r.HERE.parent/'evidence/successor-namespace-readback.json').read_bytes())
        output=io.StringIO()
        with patch('sys.stdout',output):frames.emit(value)
        lines=output.getvalue().splitlines()
        self.assertTrue(all(len(line)<=240 for line in lines));self.assertLessEqual(len(lines),10)
        self.assertEqual(parse('Output:\r\n'+'\r\n'.join(lines)),value)
        old='CPI225_SUCCESSOR '+json.dumps(value,sort_keys=True)
        self.assertGreater(len(old),250)
        with self.assertRaises(Exception):parse('Output:\r\n'+old[:250])

if __name__=='__main__':unittest.main()
