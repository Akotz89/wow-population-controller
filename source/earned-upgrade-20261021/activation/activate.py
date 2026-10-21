#!/usr/bin/env python3
"""Task-specific, pinned separate-release cutover; no database mutation or build commands."""
import argparse
from contextlib import contextmanager
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time

from soap_transport import read_credentials, soap_command

HERE = Path(__file__).resolve().parent
OLD = Path('/srv/wow/releases/cpi225-selected-modules-20261011')
NEW = Path('/srv/wow/releases/cpi225-native-earned-upgrade-20261021')
BASE = Path('/srv/wow/releases/cpi225-party-direct-inventory-20260916-v75-mail-batch/population')
RUN = Path('/srv/wow/work/core1-native-earned-upgrade-delivery-20261021')
UNIT = 'azerothcore-worldserver.service'
DEPS = ('azerothcore-authserver.service', 'azerothcore-mysql.service')
DROP = Path('/etc/systemd/system') / (UNIT + '.d/90-core1-native-earned-upgrade.conf')
LOCK = Path('/run/azerothcore-backup.lock')
OLD_SHA = '6331224bc36ec4d6778d69ca55549a5444d13c7a40ea283f01943f5b92a1f514'
NEW_SHA = '0e1fd05b210d4ba080c9d0f07a81cd7ebe3feba8686a1d212c9b31b7eeb5934d'
PINS_SHA = 'ff4de6aa594865780cbf5fd2e713e2f9374d2ed115c42e840eebaa38c4da8613'
SCHEMAS = ['cpi225_' + x + '_v7' for x in ('auth', 'characters', 'playerbots', 'site', 'world')]
CONFIG_PROPS = ('BindReadOnlyPaths', 'WorkingDirectory', 'User', 'Group', 'Restart', 'FragmentPath', 'DropInPaths')


class Refusal(RuntimeError):
    pass


def need(ok, code):
    if not ok:
        raise Refusal(code)


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def command(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    need(result.returncode == 0, 'command-failed-' + Path(argv[0]).name)
    return result.stdout.strip()


def regular(path):
    path = Path(path).absolute()
    s = path.lstat()
    need(path.resolve() == path and stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'nonregular-or-aliased-file')
    return s


def write(path, data, mode=0o600, replace=False):
    """Atomic/fsynced private output; exclusive unless this is an owned replacement."""
    path = Path(path)
    need(replace or not path.exists(), 'output-already-exists')
    fd, tmp = tempfile.mkstemp(prefix='.core1-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def package_pins():
    need(sha(HERE / 'pins.json') == PINS_SHA, 'pins-drift')
    pins = json.loads((HERE / 'pins.json').read_text())
    need(len(pins['members']) == 25, 'pin-count')
    return pins


def validate_package(path):
    """No filesystem writes until the complete whitelist, every byte and archive pass."""
    pins = package_pins()
    s = regular(path)
    need(s.st_size == pins['package']['bytes'] and sha(path) == pins['package']['sha256'], 'package-identity')
    seen = set()
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name
            need(member.isreg() and not member.linkname and not member.pax_headers, 'archive-nonregular')
            need(name == str(PurePosixPath(name)) and not name.startswith('/') and '\\' not in name
                 and '..' not in PurePosixPath(name).parts, 'archive-path')
            need(name not in seen, 'archive-duplicate')
            need(name in pins['members'], 'archive-unlisted-member')
            expected = pins['members'][name]
            need(member.size == expected['bytes'] and member.mode == int(expected['mode'], 8), 'archive-size-or-mode')
            with archive.extractfile(member) as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            need(digest == expected['sha256'], 'archive-member-hash')
            seen.add(name)
    need(seen == set(pins['members']), 'archive-missing-member')
    return pins


def tree(root):
    """Do not follow policy/state symlinks or copy their underlying data."""
    root = Path(root)
    need(root.is_dir() and not root.is_symlink(), 'tree-root')
    result = {}
    def visit(directory):
        for path in sorted(directory.iterdir()):
            rel = str(path.relative_to(root))
            s = path.lstat()
            value = {'mode': stat.S_IMODE(s.st_mode), 'uid': s.st_uid, 'gid': s.st_gid}
            if path.is_symlink():
                value['link'] = os.readlink(path)
            elif path.is_dir():
                value['dir'] = True
                visit(path)
            else:
                need(stat.S_ISREG(s.st_mode), 'tree-special-file')
                value.update(sha256=sha(path), bytes=s.st_size)
            result[rel] = value
    visit(root)
    return result


def config_set(text, values):
    lines = text.splitlines()
    for key, value in values.items():
        pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
        indices = [i for i, line in enumerate(lines) if pattern.match(line)]
        need(len(indices) <= 1, 'duplicate-config-key')
        if indices:
            lines[indices[0]] = key + ' = ' + value
        else:
            lines.append(key + ' = ' + value)
    return '\n'.join(lines) + '\n'


def drop_bytes(bindings):
    entries = shlex.split(bindings)
    old_modules = str(OLD / 'etc/modules') + ':/srv/wow/server/etc/modules'
    need(sum(entry.removesuffix(':rbind') == old_modules for entry in entries) == 1, 'original-module-bind-absent')
    entries = [entry.replace(old_modules, str(NEW / 'etc/modules') + ':/srv/wow/server/etc/modules') for entry in entries]
    entries += [str(NEW / 'python') + ':' + str(NEW / 'python'),
                str(NEW / 'population/python') + ':' + str(NEW / 'population/python')]
    return (f'[Service]\nExecStart=\nExecStart={NEW}/bin/worldserver -c {NEW}/etc/worldserver.conf\n'
            f'WorkingDirectory={NEW}/bin\nBindReadOnlyPaths=\n'
            'BindReadOnlyPaths=' + ' '.join(entries) + '\n').encode()


class Linux:
    @contextmanager
    def lock(self):
        regular(LOCK)
        fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def props(self, unit=UNIT):
        names = CONFIG_PROPS + ('MainPID', 'ActiveState', 'SubState', 'InvocationID', 'NRestarts', 'ExecStart')
        args = ['systemctl', 'show', unit]
        for name in names:
            args.extend(('-p', name))
        return dict(line.split('=', 1) for line in command(args).splitlines())

    def process(self, pid):
        root = Path('/proc') / str(pid)
        def ticks():
            return int((root / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        first = ticks()
        value = {'pid': pid, 'startticks': first, 'sha256': sha(root / 'exe'),
                 'exe': os.readlink(root / 'exe'), 'cwd': os.readlink(root / 'cwd'),
                 'argv': [part.decode() for part in (root / 'cmdline').read_bytes().split(b'\0') if part]}
        need(ticks() == first, 'process-reused')
        return value

    def world(self, prefix, expected):
        props = self.props()
        pid = int(props['MainPID'])
        proc = self.process(pid) if pid else None
        if proc:
            need(proc['exe'] == str(prefix / 'bin/worldserver') and proc['cwd'] == str(prefix / 'bin'), 'world-path-drift')
            need(proc['argv'] in ([str(prefix / 'bin/worldserver'), '-c', str(prefix / 'etc/worldserver.conf')],
                                 [str(prefix / 'bin/worldserver'), '--config', str(prefix / 'etc/worldserver.conf')]), 'world-config-argv')
            need(proc['sha256'] == expected, 'world-running-hash')
        need(sha(prefix / 'bin/worldserver') == expected, 'world-disk-hash')
        return {'props': props, 'process': proc}

    def deps(self):
        value = {}
        for unit in DEPS:
            props = self.props(unit)
            need(props['ActiveState'] == 'active', 'dependency-inactive')
            value[unit] = {'pid': props['MainPID'], 'invocation': props['InvocationID'],
                           'process': self.process(int(props['MainPID']))}
        container = json.loads(command(['podman', 'inspect', 'azerothcore-mysql-cpi225-v7', '--format', '{{json .}}']))
        need(container['State']['Running'] is True, 'mysql-container-stopped')
        value['mysql_container'] = {'id': container['Id'], 'image': container['Image'],
                                    'process': self.process(container['State']['Pid'])}
        return value

    def sql(self, query):
        need(query.startswith('SELECT '), 'non-readonly-sql')
        return command(['podman', 'exec', 'azerothcore-mysql-cpi225-v7', 'sh', '-c',
                        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --batch --skip-column-names --connect-timeout=5 -e "$1"',
                        'core1-readonly', query])

    def soap(self, text):
        need(text in ('server info', 'python status'), 'non-readonly-soap')
        value = soap_command(text, read_credentials('/etc/azerothcore/cpi188-cast.env'), timeout=20)
        need(value['ok'], 'soap-fault')
        return value['response']

    def idle(self, active=True):
        names = self.sql("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'cpi225%_v7' ORDER BY SCHEMA_NAME;").splitlines()
        need(names == SCHEMAS, 'schema-identity')
        chars = int(self.sql('SELECT COUNT(*) FROM cpi225_characters_v7.characters WHERE online<>0;'))
        accounts = int(self.sql('SELECT COUNT(*) FROM cpi225_auth_v7.account WHERE online<>0;'))
        sockets = command(['ss', '-Hnt', 'state', 'established', '( sport = :8085 )']).splitlines()
        connected = None
        if active:
            match = re.search(r'Connected players:\s*(\d+)', self.soap('server info'), re.I)
            need(match is not None, 'soap-player-count-absent')
            connected = int(match[1])
        need(chars == accounts == len(sockets) == 0 and connected in (None, 0), 'realm-not-idle')
        return {'characters': chars, 'accounts': accounts, 'sockets': len(sockets), 'soap_connected': connected}

    def writers(self):
        need(not any(UNIT in line for line in command(['systemctl', 'list-jobs', '--no-legend', '--no-pager']).splitlines()), 'pending-world-job')
        ancestors = {os.getpid()}
        parent = os.getppid()
        while parent > 1:
            ancestors.add(parent)
            try:
                parent = int((Path('/proc') / str(parent) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
            except FileNotFoundError:
                break
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) in ancestors:
                continue
            try:
                argv = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except FileNotFoundError:
                continue
            try:
                for fd in (entry / 'fd').iterdir():
                    try:
                        target = os.readlink(fd)
                        protected = (target == str(OLD / 'bin/worldserver') or target == str(OLD / 'etc/worldserver.conf')
                                     or any(target.startswith(str(root) + '/') for root in
                                            (OLD / 'etc/modules', OLD / 'python', NEW / 'etc', NEW / 'python', NEW / 'population/python')))
                        if protected:
                            flags = next(line.split()[1] for line in (entry / 'fdinfo' / fd.name).read_text().splitlines()
                                         if line.startswith('flags:'))
                            need(int(flags, 8) & 3 == 0, 'open-code-config-writer')
                    except FileNotFoundError:
                        continue
            except FileNotFoundError:
                continue
            need(not (UNIT in argv and re.search(r'\b(systemctl|busctl)\b', argv)), 'competing-service-owner')
            need(not re.search(r'(activate_[^ /]*\.py|activate\.py (?:stage|activate|rollback)|wow-realm .*deploy|rsync .*?/srv/wow|install .*?/srv/wow)', argv), 'competing-runtime-writer')

    def service_files(self):
        props = self.props()
        paths = [props['FragmentPath']] + props['DropInPaths'].split()
        return {name: sha(name) for name in paths if name and name != str(DROP)}

    def verify_bindings(self, prefix, world):
        source = str(prefix / 'etc/modules')
        destination = '/srv/wow/server/etc/modules'
        configured = shlex.split(world['props']['BindReadOnlyPaths'])
        need(any(entry.removesuffix(':rbind') == source + ':' + destination for entry in configured), 'module-bind-config')
        proc = world['process']
        need(proc is not None, 'module-bind-process')
        matches = []
        for line in (Path('/proc') / str(proc['pid']) / 'mountinfo').read_text().splitlines():
            fields = line.split()
            if len(fields) > 6 and fields[4] == destination:
                matches.append(fields)
        need(len(matches) == 1, 'module-bind-missing')
        device = Path(source).stat().st_dev
        need(matches[0][3] == source and matches[0][2] == f'{os.major(device)}:{os.minor(device)}'
             and 'ro' in matches[0][5].split(','), 'module-bind-identity')

    def original(self):
        world = self.world(OLD, OLD_SHA)
        need(world['props']['ActiveState'] == 'active' and world['process'], 'world-not-active')
        need(world['props']['NRestarts'] == '0', 'world-restart-history')
        self.verify_bindings(OLD, world)
        self.writers()
        return {'world': world, 'deps': self.deps(), 'etc': tree(OLD / 'etc'),
                'python': tree(OLD / 'python'), 'population_code': tree(BASE / 'python'), 'policy': tree(BASE / 'policy'),
                'service_files': self.service_files(), 'idle': self.idle()}

    def unchanged(self, baseline):
        original_executable()
        need(self.deps() == baseline['deps'], 'dependency-drift')
        need(tree(OLD / 'etc') == baseline['etc'] and tree(OLD / 'python') == baseline['python'], 'original-code-config-drift')
        need(tree(BASE / 'python') == baseline['population_code'], 'population-base-code-drift')
        need(tree(BASE / 'policy') == baseline['policy'], 'population-policy-drift')
        need(self.service_files() == baseline['service_files'], 'original-service-files-drift')

    def stop(self):
        command(['systemctl', 'stop', UNIT], timeout=240)
        props = self.props()
        need(props['MainPID'] == '0' and props['ActiveState'] in ('inactive', 'failed'), 'world-not-stopped')

    def exit_identity(self):
        return dict(line.split('=', 1) for line in command(['systemctl', 'show', UNIT,
            '-p', 'ExecMainPID', '-p', 'ExecMainStartTimestampMonotonic', '-p', 'InvocationID']).splitlines())

    def start(self, prefix, expected, previous_invocation):
        original_executable()
        regular(prefix / 'bin/worldserver')
        need(sha(prefix / 'bin/worldserver') == expected, 'start-executable-drift')
        begin = time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf('SC_CLK_TCK')
        command(['systemctl', '--no-block', 'start', UNIT])
        owned = None
        while True:
            current = self.world(prefix, expected)
            props, proc = current['props'], current['process']
            if proc:
                need(proc['startticks'] >= int(begin) - 1 and props['InvocationID'] != previous_invocation, 'start-not-owned')
                if owned:
                    need(same_owner(current, owned), 'start-process-drift')
                else:
                    owned = current
                current['exit_custody'] = self.exit_identity()
                need(current['exit_custody']['ExecMainPID'] == str(proc['pid'])
                     and current['exit_custody']['InvocationID'] == props['InvocationID']
                     and int(current['exit_custody']['ExecMainStartTimestampMonotonic']) > 0, 'start-exit-custody-drift')
                self.start_owned = current
            need(props['ActiveState'] not in ('failed', 'inactive'), 'world-start-failed')
            if props['ActiveState'] == 'active' and proc:
                return current
            time.sleep(1)

    def ready(self, owned, prefix, expected, baseline):
        while True:
            need(same_owner(self.world(prefix, expected), owned), 'readiness-process-drift')
            self.unchanged(baseline)
            try:
                self.idle()
                status = self.soap('python status')
            except (OSError, TimeoutError, Refusal) as error:
                if isinstance(error, Refusal) and str(error) not in ('soap-fault',):
                    raise
                time.sleep(1)
                continue
            need('Python Engine: Ready' in status and 'Reloading: No' in status, 'python-not-ready')
            need(same_owner(self.world(prefix, expected), owned), 'readiness-process-drift')
            self.verify_bindings(prefix, owned)
            return {'world': owned, 'python_ready': True, 'gameplay_verified': False}


def same_owner(first, second):
    return (first['process'] == second['process'] and all(
        first['props'][key] == second['props'][key] for key in CONFIG_PROPS + ('InvocationID', 'NRestarts')))


def original_executable():
    regular(OLD / 'bin/worldserver')
    need(sha(OLD / 'bin/worldserver') == OLD_SHA, 'original-executable-drift')


def stopped_owner(current, owned, epoch):
    return (owned['process'] is not None and current['process'] is None
            and current['props']['MainPID'] == '0'
            and current['props']['ActiveState'] in ('inactive', 'failed')
            and current['props']['SubState'] in ('dead', 'failed')
            and all(current['props'][key] == owned['props'][key]
                    for key in CONFIG_PROPS + ('InvocationID', 'NRestarts'))
            and owned.get('exit_custody') is not None and epoch == owned['exit_custody']
            and epoch['ExecMainPID'] == str(owned['process']['pid']))


def candidate_tree():
    need((NEW / 'logs').is_dir() and not (NEW / 'logs').is_symlink(), 'candidate-log-path')
    return {name: value for name, value in tree(NEW).items()
            if (name != 'logs' and not name.startswith('logs/') and '__pycache__' not in Path(name).parts
                and not name.endswith('.pyc')) or 'link' in value}


def expected_drop(saved):
    return drop_bytes(saved['binding']['baseline']['world']['props']['BindReadOnlyPaths'])


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    need(not target.is_symlink(), 'copy-target-link')
    if target.exists():
        need(not target.is_symlink() and sha(target) == sha(source), 'partial-stage-conflict')
    else:
        shutil.copy2(source, target)


def stage(backend, package):
    pins = validate_package(package)
    before = backend.original()
    for identity in (before['etc'], before['python'], before['population_code'], before['policy']):
        need(not any('link' in value for value in identity.values()), 'source-code-config-link')
    drop_bytes(before['world']['props']['BindReadOnlyPaths'])
    need(not DROP.exists() and not DROP.is_symlink(), 'owned-dropin-already-exists')
    binding = {'schema': 'core1-native-earned-upgrade-stage-v1', 'old': str(OLD), 'new': str(NEW),
               'package': pins['package'], 'baseline': before, 'performer': sha(Path(__file__)),
               'pins': PINS_SHA, 'soap_helper': sha(HERE / 'soap_transport.py')}
    marker = RUN / 'stage-owner.private.json'
    if marker.exists():
        saved = json.loads(marker.read_text())
        need(saved == binding, 'stage-bindings-drift')
    else:
        need(not NEW.exists() and not NEW.is_symlink(), 'conflicting-release')
        write(marker, canonical(binding))
    if (RUN / 'staged.private.json').exists():
        saved = json.loads((RUN / 'staged.private.json').read_text())
        need(saved['candidate'] == candidate_tree(), 'staged-tree-drift')
        return {'staged': True, 'resumed': True, 'candidate_world': NEW_SHA}
    NEW.mkdir(mode=0o700, exist_ok=True)
    need(NEW.resolve() == NEW, 'candidate-release-alias')
    # On resume, existing bytes must be either the original copy or their exact accepted overlay.
    world_text = config_set((OLD / 'etc/worldserver.conf').read_text(), {
        'LogsDir': '"' + str(NEW / 'logs') + '"', 'Updates.EnableDatabases': '0'})
    python_text = config_set((OLD / 'etc/modules/mod_python_engine.conf').read_text(), {
        'Python.Enabled': '1', 'Python.Population.Enabled': '0',
        'Python.ScriptsPath': '"' + str(NEW / 'python') + '"'})
    desired = {NEW / name: pin['sha256'] for name, pin in pins['members'].items()}
    desired.update({NEW / 'etc/worldserver.conf': hashlib.sha256(world_text.encode()).hexdigest(),
                    NEW / 'etc/modules/mod_python_engine.conf': hashlib.sha256(python_text.encode()).hexdigest()})
    expected_paths = {'logs'} | set(pins['members']) | {'population/' + x.name for x in BASE.iterdir() if x.name not in ('python', 'policy')}
    for source, relative in ((OLD / 'etc', 'etc'), (OLD / 'python', 'python'), (BASE / 'python', 'population/python'), (BASE / 'policy', 'population/policy')):
        for path in [source] + sorted(source.rglob('*')):
            target = NEW / relative / path.relative_to(source)
            expected_paths.add(str(target.relative_to(NEW)))
    for name in list(expected_paths):
        expected_paths.update(str(p) for p in Path(name).parents if str(p) != '.')
    partial = tree(NEW)
    need(set(partial) <= expected_paths, 'partial-stage-unlisted-path')
    pointer_names = {'population/' + path.name for path in BASE.iterdir() if path.name not in ('python', 'policy')}
    need(not any('link' in value and name not in pointer_names for name, value in partial.items()), 'partial-stage-code-link')
    for source, relative in ((OLD / 'etc', 'etc'), (OLD / 'python', 'python'), (BASE / 'python', 'population/python'), (BASE / 'policy', 'population/policy')):
        for path in [source] + sorted(source.rglob('*')):
            target = NEW / relative / path.relative_to(source)
            need(not target.is_symlink(), 'partial-stage-link')
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif target.exists():
                regular(target)
                need(sha(target) in (sha(path), desired.get(target)), 'partial-stage-conflict')
            else:
                copy_file(path, target)
    for path in BASE.iterdir():
        if path.name not in ('python', 'policy'):
            target = NEW / 'population' / path.name
            if target.is_symlink():
                need(target.resolve() == path.resolve(), 'population-pointer-drift')
            else:
                need(not target.exists(), 'population-pointer-conflict')
                target.symlink_to(path, target_is_directory=path.is_dir())
    copy_file(OLD / 'bin/worldserver', RUN / 'original-worldserver.private')
    need(sha(RUN / 'original-worldserver.private') == OLD_SHA, 'recovery-world-hash')
    os.chmod(RUN / 'original-worldserver.private', 0o600)
    # The archive is pinned again before projection; write bytes manually, never extractall.
    validate_package(package)
    with tarfile.open(package, 'r:gz') as archive:
        for member in archive:
            target = NEW / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source:
                data = source.read()
            need(hashlib.sha256(data).hexdigest() == pins['members'][member.name]['sha256'], 'projection-drift')
            if target.exists():
                regular(target)
                if sha(target) != pins['members'][member.name]['sha256']:
                    base_source = BASE / member.name.removeprefix('population/')
                    need(member.name.startswith('population/python/') and base_source.is_file()
                         and sha(target) == sha(base_source), 'partial-package-conflict')
                    write(target, data, member.mode, replace=True)
            else:
                write(target, data, member.mode)
    world = NEW / 'etc/worldserver.conf'
    regular(world)
    write(world, world_text.encode(), replace=True)
    python_conf = NEW / 'etc/modules/mod_python_engine.conf'
    regular(python_conf)
    write(python_conf, python_text.encode(), replace=True)
    (NEW / 'logs').mkdir(exist_ok=True)
    props = before['world']['props']
    user = pwd.getpwnam(props['User']) if props['User'] else pwd.getpwuid(0)
    gid = grp.getgrnam(props['Group']).gr_gid if props['Group'] else user.pw_gid
    for root, dirs, files in os.walk(NEW, followlinks=False):
        os.chown(root, user.pw_uid, gid)
        for name in dirs + files:
            path = Path(root) / name
            if not path.is_symlink():
                os.chown(path, user.pw_uid, gid)
    os.chmod(NEW, 0o750)
    os.chmod(NEW / 'etc', 0o700)
    os.chmod(world, 0o600)
    os.chmod(python_conf, 0o600)
    need(sha(NEW / 'bin/worldserver') == NEW_SHA, 'candidate-world-hash')
    for name, pin in pins['members'].items():
        need(sha(NEW / name) == pin['sha256'], 'staged-member-drift')
    backend.unchanged(before)
    need(backend.world(OLD, OLD_SHA) == before['world'], 'original-world-drift')
    saved = {'binding': binding, 'candidate': candidate_tree(), 'dropin_sha256': hashlib.sha256(drop_bytes(before['world']['props']['BindReadOnlyPaths'])).hexdigest()}
    write(RUN / 'staged.private.json', canonical(saved))
    return {'staged': True, 'resumed': False, 'candidate_world': NEW_SHA, 'members_verified': len(pins['members'])}


def staged(backend):
    original_executable()
    regular(RUN / 'staged.private.json')
    value = json.loads((RUN / 'staged.private.json').read_text())
    need(value['binding']['performer'] == sha(Path(__file__)) and value['binding']['pins'] == PINS_SHA
         and value['binding']['soap_helper'] == sha(HERE / 'soap_transport.py'), 'performer-drift')
    need(value['candidate'] == candidate_tree(), 'candidate-tree-drift')
    need(sha(RUN / 'original-worldserver.private') == OLD_SHA, 'recovery-world-drift')
    backend.unchanged(value['binding']['baseline'])
    return value


def recover(backend, saved, owned):
    original_executable()
    baseline = saved['binding']['baseline']
    backend.unchanged(baseline)
    need(DROP.is_file() and not DROP.is_symlink() and DROP.read_bytes() == expected_drop(saved), 'recovery-dropin-not-owned')
    current = backend.world(NEW, NEW_SHA)
    need(same_owner(current, owned) or stopped_owner(current, owned, backend.exit_identity()),
         'recovery-process-not-owned')
    backend.writers()
    backend.idle(active=current['process'] is not None and current['props']['ActiveState'] == 'active')
    backend.stop()
    backend.unchanged(baseline)
    DROP.unlink()
    command(['systemctl', 'daemon-reload'])
    need(backend.service_files() == baseline['service_files'], 'recovery-original-dropins-drift')
    restored = backend.start(OLD, OLD_SHA, current['props']['InvocationID'])
    return backend.ready(restored, OLD, OLD_SHA, baseline)


def activate(backend):
    saved = staged(backend)
    baseline = saved['binding']['baseline']
    need(not (RUN / 'activation.private.json').exists(), 'activation-already-attempted')
    need(not DROP.exists() and not DROP.is_symlink(), 'dropin-conflict')
    current = backend.original()
    need(current == baseline, 'original-identity-drift')
    write(RUN / 'activation.private.json', canonical({'before': current, 'start_requested': False}))
    result = {'activated': False, 'recovered': False, 'gameplay_verified': False, 'first_failure': None, 'recovery_failure': None}
    stop_requested = False
    owned = None
    try:
        # Human activity and process identity are checked immediately before stop, under the lock.
        backend.unchanged(baseline)
        need(candidate_tree() == saved['candidate'], 'last-candidate-drift')
        backend.writers()
        need(backend.world(OLD, OLD_SHA) == baseline['world'], 'last-world-drift')
        backend.idle()
        stop_requested = True
        backend.stop()
        backend.unchanged(baseline)
        write(DROP, expected_drop(saved), mode=0o644)
        command(['systemctl', 'daemon-reload'])
        owned = backend.start(NEW, NEW_SHA, baseline['world']['props']['InvocationID'])
        write(RUN / 'candidate-custody.private.json', canonical(owned))
        after = backend.ready(owned, NEW, NEW_SHA, baseline)
        result.update(activated=True, runtime=after)
    except Exception as error:
        result['first_failure'] = safe_error(error)
        write(RUN / 'first-failure.private.json', canonical(result['first_failure']))
        try:
            if owned is None:
                owned = getattr(backend, 'start_owned', None)
            if owned is not None:
                result['recovery'] = recover(backend, saved, owned)
                result['recovered'] = True
            elif stop_requested:
                # No candidate process is adoptable after an uncertain start: recover only a proven stopped service.
                props = backend.props()
                need(props['MainPID'] == '0' and props['ActiveState'] in ('inactive', 'failed'), 'unowned-start-recovery-refused')
                backend.unchanged(baseline)
                backend.writers()
                backend.idle(active=False)
                if DROP.exists():
                    need(DROP.read_bytes() == expected_drop(saved), 'recovery-dropin-drift')
                    DROP.unlink()
                    command(['systemctl', 'daemon-reload'])
                restored = backend.start(OLD, OLD_SHA, baseline['world']['props']['InvocationID'])
                result['recovery'] = backend.ready(restored, OLD, OLD_SHA, baseline)
                result['recovered'] = True
        except Exception as recovery_error:
            result['recovery_failure'] = safe_error(recovery_error)
    write(RUN / 'activation-result.private.json', canonical(result))
    return result


def safe_error(error):
    return {'type': type(error).__name__, 'code': str(error) if isinstance(error, Refusal) else 'external-operation-failed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('validate', 'stage', 'check', 'activate', 'rollback'))
    parser.add_argument('--package', type=Path, default=RUN / 'candidate.tar.gz')
    args = parser.parse_args()
    try:
        if args.mode == 'validate':
            pins = validate_package(args.package)
            result = {'package': pins['package'], 'members_verified': len(pins['members']), 'writes': False}
        else:
            need(os.geteuid() == 0, 'root-required')
            need(RUN.is_dir() and RUN.resolve() == RUN and RUN.stat().st_mode & 0o077 == 0, 'private-run-required')
            backend = Linux()
            with backend.lock():
                if args.mode == 'stage':
                    result = stage(backend, args.package)
                elif args.mode == 'check':
                    result = {'staged': staged(backend) is not None, 'original': backend.original(), 'writes': False}
                elif args.mode == 'activate':
                    result = activate(backend)
                else:
                    saved = staged(backend)
                    regular(RUN / 'candidate-custody.private.json')
                    owned = json.loads((RUN / 'candidate-custody.private.json').read_text())
                    result = {'recovered': True, 'recovery': recover(backend, saved, owned), 'gameplay_verified': False}
                    write(RUN / 'rollback-result.private.json', canonical(result))
        # Configuration contents, credentials and command output are never printed.
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get('first_failure') else 0
    except Exception as error:
        print(json.dumps({'error': safe_error(error)}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
