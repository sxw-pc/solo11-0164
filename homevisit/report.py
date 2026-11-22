import json
from datetime import datetime

def _worker_summary(db,job,segments):
    """Per-worker on-site time (both routes share the same physical presence)
    and per-route responsible/billable/frozen minutes."""
    receipts=db.execute('SELECT * FROM visit_receipt WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    site={}
    for receipt in receipts:
        site.setdefault(receipt['worker'],{'worker':receipt['worker'],'site_minutes':0,
                                           'routes':{}})
    starts={};ends={};pauses={};resumes={}
    for receipt in receipts:
        if receipt['kind']=='start':starts.setdefault(receipt['worker'],[]).append(receipt)
        elif receipt['kind']=='end':ends.setdefault(receipt['worker'],[]).append(receipt)
        elif receipt['kind']=='pause':pauses.setdefault(receipt['worker'],[]).append(receipt)
        elif receipt['kind']=='resume':resumes.setdefault(receipt['worker'],[]).append(receipt)
    for worker,rows in site.items():
        first=min(starts.get(worker,[]),key=lambda r:r['at'],default=None)
        last=max(ends.get(worker,[]),key=lambda r:r['at'],default=None)
        if first and last:
            total=int((datetime.fromisoformat(last['at'])-datetime.fromisoformat(first['at'])).total_seconds()//60)
            paused=0
            for a,b in zip(pauses.get(worker,[]),resumes.get(worker,[])):
                if a['at']<b['at']:
                    paused+=int((datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds()//60)
            rows['site_minutes']=max(0,total-paused)
    for seg in segments:
        bucket=site.setdefault(seg['worker'],{'worker':seg['worker'],'site_minutes':0,'routes':{}})
        route=bucket['routes'].setdefault(seg['route'],{'responsible_minutes':0,'payable':0,'frozen':0})
        route['responsible_minutes']+=seg['minutes']
        route['payable']+=seg['payable']
        route['frozen']+=seg['frozen']
    return list(site.values())

def _relay_workbench(db,job):
    requests=db.execute('SELECT * FROM relay_request WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    if not requests:return None
    handovers=db.execute('SELECT * FROM handover_receipt WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    confirmations=db.execute('SELECT * FROM responsibility_confirmation WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    points=[]
    for req in requests:
        hand=next((h for h in handovers if h['at']==req['at'] and h['from_worker']==req['from_worker'] and h['to_worker']==req['to_worker']),None)
        points.append({'at':req['at'],'relay_request_id':req['event_id'],'client_approved':req['client_approved'],
                       'from_worker':req['from_worker'],'to_worker':req['to_worker'],
                       'handover_id':hand['event_id'] if hand else None,
                       'from_signed':hand['from_signed'] if hand else None,
                       'to_signed':hand['to_signed'] if hand else None})
    return {'points':points,
            'confirmations':[dict(c) for c in confirmations],
            'assignments':[dict(r) for r in db.execute('SELECT * FROM assignment WHERE job=? ORDER BY at,event_id',(job,))],
            'certificates':[dict(r) for r in db.execute('SELECT * FROM worker_certificate ORDER BY worker,event_id')
                            if db.execute('SELECT 1 FROM assignment WHERE job=? AND worker=?',(job,r['worker'])).fetchone()],
            'revocations':[dict(r) for r in db.execute('SELECT * FROM cert_revocation ORDER BY at,event_id')
                           if db.execute('SELECT 1 FROM assignment WHERE job=? AND worker=?',(job,r['worker'])).fetchone()],
            'acceptances':[dict(r) for r in db.execute('SELECT * FROM task_acceptance WHERE job=? ORDER BY event_id',(job,))]}

def report(db):
    jobs=[]
    for row in db.execute('SELECT DISTINCT job FROM service_contract ORDER BY job'):
        job=row['job'];item={'job':job}
        for table,key in [('contract_decision','contract'),('dispatch_decision','dispatch'),('settlement_decision','settlement')]:
            found=db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone()
            item[key]=dict(found) if found else None
        segments=[dict(r) for r in db.execute('SELECT * FROM service_segment WHERE job=? ORDER BY route,idx',(job,))]
        item['segments']=segments
        item['workers']=_worker_summary(db,job,segments)
        item['relay_workbench']=_relay_workbench(db,job)
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE job=? ORDER BY id',(job,))]
        jobs.append(item)
    return {'jobs':jobs,'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}

def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
