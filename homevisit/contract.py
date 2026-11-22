from .source import record
from .store import update

def effective_detail(db,job):
    rows=db.execute('SELECT * FROM service_contract WHERE job=? ORDER BY version DESC,event_id',(job,)).fetchall()
    if not rows:return None,None,'missing',{}
    top=rows[0];same=[r for r in rows if r['version']==top['version']]
    sources={key:top['event_id'] for key in ('client','address','start','end','task','rate','signed_by','version')}
    if len({(r['address'],r['start'],r['end'],r['task'],r['rate']) for r in same})>1:return None,top,'conflict',sources
    changes=db.execute('SELECT * FROM contract_change WHERE job=? ORDER BY version,event_id',(job,)).fetchall()
    current=dict(top)
    for change in changes:
        if change['version']<=top['version']:continue
        if change['signed_by']!=top['client']:continue
        if change['kind']=='cancel':return None,top,'cancelled',sources
        for key in ('address','start','end','task'):
            if change[key] is not None:current[key]=change[key];sources[key]=change['event_id']
    return current,top,'valid',sources

def effective(db,job):
    current,top,condition,_=effective_detail(db,job)
    return current,top,condition

def decide(db,job):
    contract,top,condition=effective(db,job)
    if condition=='missing':state,reason,contract_id='pending','signed service contract missing',None
    elif condition=='conflict':state,reason,contract_id='frozen','same-version contract conflict',None
    elif condition=='cancelled':state,reason,contract_id='cancelled','client cancelled current service window',None
    elif top['signed_by']!=top['client'] or contract['start']>=contract['end']:
        state,reason,contract_id='blocked','client signature or service window invalid',None
    else:state,reason,contract_id='active','client task, address and service window confirmed',top['event_id']
    result=update(db,'contract_decision',job,state=state,reason=reason,contract_id=contract_id)
    from .dispatch import decide as dispatch_decide
    dispatch_decide(db,job)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('contracts',[]):
            if record(db,row['event_id'],'contract',row):
                db.execute('INSERT INTO service_contract VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['version'],row['client'],row['address'],row['start'],row['end'],row['task'],row['rate'],row['signed_by']))
            touched.add(row['job'])
        for row in data.get('changes',[]):
            if record(db,row['event_id'],'change',row):
                db.execute('INSERT INTO contract_change VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['version'],row['kind'],row.get('address'),row.get('start'),row.get('end'),row.get('task'),row['signed_by']))
            touched.add(row['job'])
        touched.update(r[0] for r in db.execute('SELECT DISTINCT job FROM assignment'))
        return [decide(db,job) for job in sorted(touched)]
