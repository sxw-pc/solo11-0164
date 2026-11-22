from .source import record
from .store import update
from .contract import effective
from . import segment

def _certificate_active(db,worker,task,start,end):
    cert=db.execute('SELECT * FROM worker_certificate WHERE worker=? AND task=? AND valid_from<=? AND valid_to>=? ORDER BY valid_to DESC,event_id',
                    (worker,task,start,end)).fetchone()
    if not cert or cert['issuer']!='agency':return None
    if db.execute('SELECT 1 FROM cert_revocation WHERE worker=? AND task=? AND at<=?',
                  (worker,task,end)).fetchone():return None
    return cert

def _peer_clash(db,assignment,contract):
    for peer in db.execute('SELECT * FROM assignment WHERE worker=? AND job<>? ORDER BY at,event_id',
                           (assignment['worker'],assignment['job'])):
        peer_contract,peer_top,peer_condition=effective(db,peer['job'])
        if not peer_contract or peer_condition!='valid' or not peer_top or peer['client_approved']!=peer_top['client']:
            continue
        if peer_contract['start']<contract['end'] and peer_contract['end']>contract['start'] and (peer['at'],peer['event_id'])<(assignment['at'],assignment['event_id']):
            return peer
    return None

def _decide_single(db,job,contract,top,assignments):
    if not assignments:
        return 'pending','worker assignment missing',None,None
    assignment=assignments[0]
    cert=_certificate_active(db,assignment['worker'],contract['task'],contract['start'],contract['end'])
    if assignment['client_approved']!=top['client']:
        return 'blocked','replacement or assignment lacks client authorization',None,None
    if assignment['replace_of'] and not db.execute('SELECT 1 FROM assignment WHERE event_id=? AND job=?',(assignment['replace_of'],job)).fetchone():
        return 'blocked','replacement does not cite existing assignment',None,None
    if not cert:
        return 'blocked','worker certificate does not cover task and entire window',None,None
    clash=_peer_clash(db,assignment,contract)
    if clash:return 'waitlist',f'worker already committed to {clash["job"]}',None,None
    return 'assigned','qualified worker reserved for client window',assignment['worker'],assignment['event_id']

def _decide_relay(db,job,contract,top):
    points,errors=segment.validate_chain(db,job,contract,top,spans={},require_presence=False)
    chain=[]
    for point in points:
        chain.extend([point['from_worker'],point['to_worker']])
    chain=list(dict.fromkeys(chain))
    assignments=db.execute('SELECT * FROM assignment WHERE job=? ORDER BY at,event_id',(job,)).fetchall()
    by_worker={}
    for row in assignments:by_worker.setdefault(row['worker'],[]).append(row)
    for worker in chain:
        rows=by_worker.get(worker,[])
        if not rows:
            errors.append(('blocked',f'relay worker {worker} has no assignment on record'))
        for row in rows:
            if row['replace_of'] and not db.execute('SELECT 1 FROM assignment WHERE event_id=? AND job=?',(row['replace_of'],job)).fetchone():
                errors.append(('blocked',f'assignment {row["event_id"]} cites a missing predecessor'))
        if not _certificate_active(db,worker,contract['task'],contract['start'],contract['end']):
            errors.append(('blocked',f'relay worker {worker} certificate does not cover task and entire window'))
        for row in rows:
            clash=_peer_clash(db,row,contract)
            if clash:
                errors.append(('blocked',f'relay worker {worker} already committed to {clash["job"]}'))
                break
    if errors:
        return 'blocked','; '.join(message for _,message in errors),None,None
    lead=points[0]['from_worker']
    lead_assignment=by_worker[lead][0]
    return 'assigned',f'client-authorized relay of {len(points)} handoff point(s) reserved',lead,lead_assignment['event_id']

def decide(db,job):
    contract,top,condition=effective(db,job)
    assignments=db.execute('SELECT * FROM assignment WHERE job=? ORDER BY at DESC,event_id',(job,)).fetchall()
    if condition!='valid' or not top or top['signed_by']!=top['client']:
        state,reason,worker,assignment_id='blocked','service contract not active',None,None
    elif db.execute('SELECT 1 FROM relay_request WHERE job=?',(job,)).fetchone():
        state,reason,worker,assignment_id=_decide_relay(db,job,contract,top)
    else:
        state,reason,worker,assignment_id=_decide_single(db,job,contract,top,assignments)
    result=update(db,'dispatch_decision',job,state=state,reason=reason,worker=worker,assignment_id=assignment_id)
    from .visit import decide as visit_decide
    visit_decide(db,job)
    return result

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('certificates',[]):
            if record(db,row['event_id'],'certificate',row):
                db.execute('INSERT INTO worker_certificate VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['worker'],row['task'],row['valid_from'],row['valid_to'],row['issuer']))
            touched.update(r[0] for r in db.execute('SELECT job FROM assignment WHERE worker=?',(row['worker'],)))
        for row in data.get('revocations',[]):
            if record(db,row['event_id'],'revocation',row):
                db.execute('INSERT INTO cert_revocation VALUES(?,?,?,?,?)',
                           (row['event_id'],row['worker'],row['task'],row['at'],row['issuer']))
            touched.update(r[0] for r in db.execute('SELECT job FROM assignment WHERE worker=?',(row['worker'],)))
        for row in data.get('assignments',[]):
            if record(db,row['event_id'],'assignment',row):
                db.execute('INSERT INTO assignment VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['worker'],row['at'],row['client_approved'],row.get('replace_of')))
            touched.add(row['job'])
        for row in data.get('relay_requests',[]):
            if record(db,row['event_id'],'relay_request',row):
                db.execute('INSERT INTO relay_request VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['at'],row['from_worker'],row['to_worker'],row['client_approved']))
            touched.add(row['job'])
        for row in data.get('handovers',[]):
            if record(db,row['event_id'],'handover',row):
                db.execute('INSERT INTO handover_receipt VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['at'],row['from_worker'],row['to_worker'],row['from_signed'],row['to_signed']))
            touched.add(row['job'])
        touched.update(r[0] for r in db.execute('SELECT DISTINCT job FROM assignment'))
        touched.update(r[0] for r in db.execute('SELECT DISTINCT job FROM relay_request'))
        return [decide(db,job) for job in sorted(touched)]
