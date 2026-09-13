"""Transactional policy config staging; existing controller remains native authority's consumer."""
import hashlib,json,os,stat,tempfile
from collections import deque
from dataclasses import fields
from pathlib import Path

POLICY_FIELDS=frozenset({'bands','rank_denominator','admission_capacity','newcomer_wave_counts','newcomer_schedule_path','newcomer_schedule_sha256','online_cap','starter_zone_online_cap','bridge_batch_size','action_batch_size','admission_batch_size','admission_interval_seconds','snapshot_weights','pending_admission_limit','quest_discovery_budget','quest_renewal_budget','quest_tick_interval_seconds','session_rotation_interval_seconds','session_rotation_max_per_tick','newcomer_session_share'})
sha=lambda data:hashlib.sha256(data).hexdigest()

def read_regular(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('nonregular policy artifact')
        return f.read()

def atomic(path,data,mode):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
        temporary=Path(f.name);f.write(data);f.flush();os.fchmod(f.fileno(),mode);os.fsync(f.fileno())
    try:os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def eligible(old,new,canonical):
    # Python-only reloads replace controller/config classes but retain shared
    # manifest, capability, state, and model modules for continuity.
    if old is None:return False
    if old._recovery_pending or old._recovery_keys:return False
    if old.manifest!=new.manifest or old.capabilities!=new.capabilities or old.state!=new.state or old.state is new.state:return False
    old_fields = {f.name for f in fields(old.config)}
    if any(
        f.name in old_fields
        and getattr(old.config, f.name) != getattr(new.config, f.name)
        for f in fields(new.config)
        if f.name not in POLICY_FIELDS
    ):
        return False
    excluded={canonical,old.config.newcomer_schedule_path,new.config.newcomer_schedule_path}-{None}
    # New controller's request-config fence is removed by stage before comparison.
    a={p:h for p,h in old._artifact_digests.items() if p not in excluded}
    b={p:h for p,h in new._artifact_digests.items() if p not in excluded}
    return a==b

def stage(root,previous_namespace,controller_type,adapter,clock):
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid!=os.geteuid() or root.stat().st_mode&0o022:raise ValueError('policy root must be service-owned and not group/world writable')
    canonical=root/'runtime-config.json';request_path=root/'.policy-reload-request.json'
    before=read_regular(canonical);request=None;selected=canonical
    if request_path.exists() or request_path.is_symlink():
        request_bytes=read_regular(request_path);request=json.loads(request_bytes)
        if set(request)!={'before_sha256','candidate_sha256','candidate_name'}:raise ValueError('request keyset invalid')
        if any(not isinstance(request[k],str) for k in request) or any(len(request[k])!=64 or any(c not in '0123456789abcdef' for c in request[k]) for k in ('before_sha256','candidate_sha256')):raise ValueError('request digest malformed')
        if request['candidate_name']!='runtime-config-'+request['candidate_sha256']+'.json':raise ValueError('candidate name is not its exact hash')
        selected=root/request['candidate_name'];candidate_bytes=read_regular(selected)
        if sha(candidate_bytes)!=request['candidate_sha256']:raise ValueError('candidate bytes changed')
        if sha(before)==request['candidate_sha256']:
            # Prior commit survived a crash; canonical bytes remain the authority.
            selected=canonical;request=None
        elif sha(before)!=request['before_sha256']:raise ValueError('request predecessor changed')
    new=controller_type.from_config_path(selected,adapter,clock)
    expected=before if request is None else candidate_bytes
    if read_regular(selected)!=expected or new._artifact_digests.get(selected)!=sha(expected):raise ValueError('config changed during candidate validation')
    new._artifact_digests.pop(selected,None)
    new._artifact_digests[canonical]=sha(before if request is None else candidate_bytes)
    old=previous_namespace.get('controller') if isinstance(previous_namespace,dict) else None
    continuous=eligible(old,new,canonical)
    if continuous:
        new._recovery_pending=False;new._recovery_keys=deque();new._last_tick_epoch=old._last_tick_epoch
    next_tick=None
    if continuous:
        prior_cadence=previous_namespace.get('cadenced')
        if prior_cadence is not None and not prior_cadence._failed:next_tick=prior_cadence._next_tick
    # Capture bytes/paths only. No old controller or namespace is retained by commit.
    def commit():
        if request is None:return
        if read_regular(canonical)!=before or read_regular(request_path)!=request_bytes or read_regular(selected)!=candidate_bytes:
            raise ValueError('policy transaction inputs changed before commit')
        mode=canonical.stat().st_mode & 0o777
        try:
            atomic(canonical,candidate_bytes,mode)
        except BaseException:
            # atomic may have replaced the name before a directory fsync failure.
            atomic(canonical,before,mode)
            raise
        # No fallible cleanup after commit: stale request is recognized by exact
        # canonical candidate hash and is removed by the external helper on proof.
    return new,commit,continuous,next_tick,sha(before if request is None else candidate_bytes)
