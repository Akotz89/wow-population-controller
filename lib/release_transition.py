"""Existing DB-first transition contract adapted to full manifests and reservations."""
from copy import deepcopy
from collections import Counter
import hashlib,json

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def prepare_transition(old,new,snapshot):
    for manifest in (old,new):
        payload=dict(manifest);claimed=payload.pop('manifest_digest')
        if claimed!=digest(payload):raise ValueError('full manifest content digest mismatch')
    if old['manifest_digest']==new['manifest_digest']:raise ValueError('distinct manifests required')
    before={r['identity_key']:r for r in old['rows']};after={r['identity_key']:r for r in new['rows']}
    if len(before)!=len(old['rows']) or len(after)!=len(new['rows']) or before.keys()!=after.keys():raise ValueError('identity set changed')
    if len({r['character_name'].casefold() for r in old['rows']})!=len(before) or len({r['guid_allocation_key'] for r in old['rows']})!=len(before):raise ValueError('duplicate identity name or GUID allocation')
    state=snapshot['state'];content=dict(state);claimed=content.pop('state_digest')
    if claimed!=digest(content):raise ValueError('state content digest mismatch')
    if state['manifest_digest'] not in (old['manifest_digest'],new['manifest_digest']):raise ValueError('unknown state release')
    accounts={a['account_key']:a for a in snapshot['population_account']}
    ledger={r['identity_key']:r for r in snapshot['population_identity']}
    if len(accounts)!=len(snapshot['population_account']) or len(ledger)!=len(snapshot['population_identity']):raise ValueError('duplicate reservation')
    guids=[r['character_guid'] for r in snapshot['population_identity'] if r['character_guid'] is not None]
    if len(set(guids))!=len(guids):raise ValueError('duplicate reserved character GUID')
    auth={r['id']:r['username'] for r in snapshot['auth_accounts']};chars={r['guid']:r for r in snapshot['characters']}
    names={r['name'].casefold():r for r in snapshot['characters']}
    if len(auth)!=len(snapshot['auth_accounts']) or len(chars)!=len(snapshot['characters']) or len(names)!=len(snapshot['characters']):raise ValueError('duplicate native account or character')
    slots=Counter(r['account_key'] for r in old['rows'])
    for key,row in before.items():
        changed=dict(after[key]);changed['appearance']=row['appearance']
        if changed!=row:raise ValueError('only uncreated appearance may change')
        if (key in ledger or key==state.get('pending_admission_key')) and after[key]!=row:raise ValueError('reserved identity changed')
        if key not in ledger and row['character_name'].casefold() in names:raise ValueError('unreserved native character exists')
    for key,account in accounts.items():
        allocated=[r for r in old['rows'] if r['account_key']==key]
        if not allocated or {r['account_name'] for r in allocated}!={account['account_name']} or account['expected_slots']!=slots[key]:raise ValueError('account allocation changed/foreign')
        if account['account_id'] is not None and auth.get(account['account_id'])!=account['account_name']:raise ValueError('account ownership mismatch')
    admitted=set();updates=[]
    for key,row in ledger.items():
        if key not in before or row['account_key'] not in accounts:raise ValueError('foreign reservation')
        identity=before[key];account=accounts[row['account_key']]
        if row['account_key']!=identity['account_key'] or row['guid_allocation_key']!=identity['guid_allocation_key']:raise ValueError('reservation keys mismatch')
        if row['wave_key']!=f"owner-level-{identity['wave_owner_level']:02d}:{identity['wave_ordinal']:04d}":raise ValueError('wave provenance mismatch')
        if row['account_id'] is not None and row['account_id']!=account['account_id']:raise ValueError('reservation account mismatch')
        if row['manifest_digest'] not in (None,old['manifest_digest'],new['manifest_digest']):raise ValueError('unknown reserved digest')
        if state['manifest_digest']==new['manifest_digest'] and row['manifest_digest']==old['manifest_digest']:raise ValueError('new state before DB acknowledgement')
        if row['character_guid'] is not None:
            char=chars.get(row['character_guid'])
            if char is None or char['account']!=row['account_id'] or char['name']!=identity['character_name'] or char['race']!=identity['race_id'] or char['class']!=identity['class_id']:raise ValueError('character ownership/template mismatch')
            for field,column in [('gender','gender'),('skin','skin'),('face','face'),('hair_style','hairStyle'),('hair_color','hairColor'),('facial_hair','facialStyle')]:
                if char[column]!=identity['appearance'][field]:raise ValueError('committed appearance mismatch')
        if type(row['admitted']) is not int or row['admitted'] not in (0,1):raise ValueError('invalid admission flag')
        if row['admitted']:
            if row['character_guid'] is None or row['manifest_digest'] is None:raise ValueError('admitted reservation incomplete')
            admitted.add(key)
        if row['manifest_digest']==old['manifest_digest']:updates.append(key)
    for key,binding in state['bindings'].items():
        row=ledger.get(key)
        if row is None or binding['account_id']!=row['account_id'] or binding['character_guid']!=row['character_guid']:raise ValueError('state binding mismatch')
    if not admitted<=state['bindings'].keys():raise ValueError('admitted identity missing state binding')
    if len(set(state['admitted_identity_keys']))!=len(state['admitted_identity_keys']) or set(state['admitted_identity_keys'])!=admitted:raise ValueError('state admissions differ from native ledger')
    pending=state.get('pending_admission_key')
    if pending is not None and pending not in before:raise ValueError('foreign pending identity')
    # Reservation may precede its state binding; retain NULL digest until normal bind.
    result=deepcopy(state);result['manifest_digest']=new['manifest_digest'];result.pop('state_digest');result['state_digest']=digest(result)
    return result,updates
