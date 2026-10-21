"""Native record release after original durable custody. All disk work is explicitly OFF TICK."""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
from capture_native_acquisition import CaptureError,read_descriptor,canonical,atomic_file,decode,native_observations

ACK_FIELDS=('record_id','account_id','character_guid','session_incarnation','strategic_generation','goal_revision','phase_digest','source_key','record_sha256','durable_sha256')


def require(value,reason):
    if not value:raise CaptureError(reason)


def hex64(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdefABCDEF' for c in value)


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class NativeCustodyAckQueue:
    """One exact durable original owner queue; no TTL, blind pruning or active-record release."""
    def __init__(self,path,document,*,in_tick):
        self.path=Path(path);self.document=copy.deepcopy(document);self.in_tick=in_tick
        self.runtime_results=None

    @classmethod
    def prepare(cls,original_descriptor,original_root,queue_path,*,in_tick):
        require(not in_tick(),'native custody preparation must be off tick')
        raw=read_descriptor(original_descriptor,Path(original_root));original=decode(raw)
        require(original.get('kind')=='native_execution' and original.get('snapshot_id')==original_descriptor.get('generation'),'native ACK original envelope custody')
        data=original['data'];identity=data['identity_key'];binding=data['binding']
        require(data.get('schema') in {'core1-original-native-quest-observations-v1','core1-original-native-quest-observations-v2'} and len(binding)==2,'native ACK original observation type')
        records={}
        for snapshot in native_observations(data):
            require(snapshot.get('identity_key')==identity and [snapshot.get('account_id'),snapshot.get('character_guid')]==binding,'native ACK original actor snapshot')
            for record in snapshot['attempts']:
                require(record.get('identity_key')==identity and [record.get('account_id'),record.get('character_guid')]==binding,'native ACK original actor record')
                old=records.get(record['record_id'])
                if old is not None and old.get('terminal') is True:
                    require(old.get('record_sha256')==record.get('record_sha256'),'native original terminal digest changed')
                records[record['record_id']]=record
        acknowledgements=[]
        for record_id,record in sorted(records.items()):
            if record.get('terminal') is not True:continue
            require(hex64(record.get('record_sha256')),'native ACK requires original native record digest')
            ack={field:copy.deepcopy(record[field]) for field in ACK_FIELDS if field!='durable_sha256'}
            ack['durable_sha256']=hashlib.sha256(raw).hexdigest()
            require(all(type(ack[f]) is int and 0<ack[f]<=2**64-1 for f in ('record_id','account_id','character_guid','session_incarnation','strategic_generation','goal_revision')),'native ACK exact original uint owner binding')
            require(hex64(ack['phase_digest']) and isinstance(ack['source_key'],str) and ack['source_key'],'native ACK original phase/source')
            acknowledgements.append(dict(ack=ack,status='pending',reason='original atomically retained; awaiting native fenced ACK'))
        # Make the authenticated original itself durable and rehash it before publishing any release queue.
        original_path=Path(original_descriptor['path'])
        if not original_path.is_absolute():original_path=Path(original_root)/original_path
        descriptor=os.open(original_path,os.O_RDONLY|os.O_NOFOLLOW)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
        sync_directory(original_path.parent)
        require(read_descriptor(original_descriptor,Path(original_root))==raw,'native ACK original changed during durable custody')
        document=dict(schema='core1-native-custody-ack-queue-v1',identity_key=identity,binding=binding,
            original_descriptor=copy.deepcopy(original_descriptor),original_root=str(Path(original_root).absolute()),
            durable_sha256=hashlib.sha256(raw).hexdigest(),snapshot_id=original['snapshot_id'],records=acknowledgements)
        atomic_file(queue_path,canonical(document)+b'\n');sync_directory(Path(queue_path).parent)
        return cls.recover(queue_path,in_tick=in_tick)

    @classmethod
    def recover(cls,queue_path,*,in_tick):
        require(not in_tick(),'native custody recovery must be off tick')
        path=Path(queue_path);require(not path.is_symlink(),'native custody queue symlink')
        document=decode(path.read_bytes())
        require(document.get('schema')=='core1-native-custody-ack-queue-v1','native custody queue schema')
        raw=read_descriptor(document['original_descriptor'],Path(document['original_root']))
        require(hashlib.sha256(raw).hexdigest()==document['durable_sha256'],'native custody recovery original digest')
        original=decode(raw);data=original['data'];require(data['identity_key']==document['identity_key'] and data['binding']==document['binding'],'native custody recovery durable owner')
        originals={r['record_id']:r for s in native_observations(data) for r in s['attempts']}
        seen=set()
        for row in document['records']:
            ack=row['ack'];require(set(ack)==set(ACK_FIELDS) and ack['record_id'] not in seen,'native custody recovery exact ACK fields/duplicate')
            seen.add(ack['record_id']);record=originals.get(ack['record_id'])
            require(record is not None and record.get('terminal') is True,'native custody cannot release active/unconfirmed record')
            require(all(ack[field]==record[field] for field in ACK_FIELDS if field!='durable_sha256') and ack['durable_sha256']==document['durable_sha256'],'native custody recovery original binding/digest mismatch')
            require(row['status'] in {'pending','applied','refused'},'native custody recovery settlement status')
        return cls(path,document,in_tick=in_tick)

    def pending(self):
        return [copy.deepcopy(row['ack']) for row in self.document['records'] if row['status']=='pending']

    def apply(self,native):
        """Admitted tick only. Exact native results are retained in memory until off-tick settlement."""
        require(self.in_tick(),'native custody ACK requires population tick')
        pending=self.pending()
        if not pending:return []
        results=native.strategic_ack_quest_acquisition_records(self.document['identity_key'],pending)
        require(isinstance(results,list) and len(results)==len(pending),'native ACK result census')
        checked=[]
        for ack,result in zip(pending,results):
            require(isinstance(result,dict) and set(result)=={'record_id','status','reason','record_sha256','durable_sha256','recovered_closed_context'} and
                result['record_id']==ack['record_id'] and result['status'] in {'released','refused'} and isinstance(result['reason'],str) and
                result['record_sha256']==ack['record_sha256'] and result['durable_sha256']==ack['durable_sha256'] and
                type(result['recovered_closed_context']) is bool,'native ACK exact result/binding/digest')
            require(result['status']!='released' or result['reason']=='caller_verified_original_durable_custody','native released ACK reason')
            checked.append(copy.deepcopy(result))
        for result in checked:
            row=next(row for row in self.document['records'] if row['ack']['record_id']==result['record_id'])
            row['status']='applied' if result['status']=='released' else 'refused'
            row['reason']=result['reason'];row['native_result']=result
        self.runtime_results=checked
        return copy.deepcopy(checked)

    def settle(self):
        """OFF TICK only; reauthenticate original before durable settlement of native results."""
        require(not self.in_tick(),'native custody settlement must be off tick')
        self.recover(self.path,in_tick=self.in_tick)
        raw=canonical(self.document)+b'\n'
        with tempfile.TemporaryDirectory(prefix='.native-ack-',dir=self.path.parent) as temporary:
            staged=Path(temporary)/'queue';atomic_file(staged,raw);os.replace(staged,self.path);sync_directory(self.path.parent)
        return dict(path=str(self.path),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
