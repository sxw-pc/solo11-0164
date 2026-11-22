from datetime import datetime
from .source import record
from .store import update
from .contract import effective

def decide(db,job):
    contract,top,condition=effective(db,job)
    dispatch=db.execute('SELECT * FROM dispatch_decision WHERE job=?',(job,)).fetchone()
    receipts=db.execute('SELECT * FROM visit_receipt WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    acceptances=db.execute('SELECT * FROM task_acceptance WHERE job=? ORDER BY event_id',(job,)).fetchall()
    minutes=payable=frozen=0
    if not contract or condition!='valid' or not dispatch or dispatch['state']!='assigned':
        state,reason='blocked','contract or worker dispatch invalid'
    elif not receipts:
        state,reason='pending','independent arrival and departure receipts missing'
    else:
        starts=[r for r in receipts if r['kind']=='start']
        ends=[r for r in receipts if r['kind']=='end']
        if not starts or not ends:
            state,reason='pending','visit lacks start or end receipt'
        else:
            start,end=starts[0],ends[-1]
            valid=(start['worker']==dispatch['worker']==end['worker'] and start['client']==top['client']==end['client']
                   and start['address']==contract['address']==end['address'] and contract['start']<=start['at']<end['at']<=contract['end'])
            if not valid:
                state,reason='disputed','worker, client, address or service window mismatch'
            else:
                total=int((datetime.fromisoformat(end['at'])-datetime.fromisoformat(start['at'])).total_seconds()//60)
                pauses=[r for r in receipts if r['kind']=='pause']
                resumes=[r for r in receipts if r['kind']=='resume']
                if len(pauses)!=len(resumes) or any(a['at']>=b['at'] or a['at']<start['at'] or b['at']>end['at'] for a,b in zip(pauses,resumes)):
                    state,reason='disputed','unmatched pause or resume receipt'
                else:
                    paused=sum(max(0,int((datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds()//60)) for a,b in zip(pauses,resumes))
                    minutes=max(0,total-paused)
                    payable=minutes*top['rate']//60
                    accepted=[r for r in acceptances if r['task']==contract['task'] and r['client']==top['client'] and r['accepted']]
                    disputed=[r for r in acceptances if r['dispute']]
                    if not accepted:
                        state,reason='pending','client has not accepted contracted task'
                        frozen,payable=payable,0
                    elif disputed:
                        state,reason='disputed','client task acceptance carries unresolved dispute'
                        frozen,payable=payable,0
                    else:state,reason='settled','qualified worker, independent visit and accepted task verified'
    result=update(db,'settlement_decision',job,state=state,reason=reason,minutes=minutes,payable=payable,frozen=frozen)
    from .material import decide as material_decide
    material_decide(db,job)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('receipts',[]):
            if record(db,row['event_id'],'receipt',row):
                db.execute('INSERT INTO visit_receipt VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['kind'],row['at'],row['address'],row['worker'],row.get('client'),row.get('task')))
            touched.add(row['job'])
        for row in data.get('acceptances',[]):
            if record(db,row['event_id'],'acceptance',row):
                db.execute('INSERT INTO task_acceptance VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['task'],int(row['accepted']),row['client'],row.get('dispute')))
            touched.add(row['job'])
        return [decide(db,job) for job in sorted(touched)]
