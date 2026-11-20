from .source import record
from .store import update
from .contract import effective

def decide(db,job):
    contract,top,condition=effective(db,job)
    assignments=db.execute('SELECT * FROM assignment WHERE job=? ORDER BY at DESC,event_id',(job,)).fetchall()
    if condition!='valid' or not top or top['signed_by']!=top['client']:
        state,reason,worker,assignment_id='blocked','service contract not active',None,None
    elif not assignments:
        state,reason,worker,assignment_id='pending','worker assignment missing',None,None
    else:
        assignment=assignments[0]
        cert=db.execute('SELECT * FROM worker_certificate WHERE worker=? AND task=? AND valid_from<=? AND valid_to>=? ORDER BY valid_to DESC',
                        (assignment['worker'],contract['task'],contract['start'],contract['end'])).fetchone()
        if assignment['client_approved']!=top['client']:
            state,reason,worker,assignment_id='blocked','replacement or assignment lacks client authorization',None,None
        elif assignment['replace_of'] and not db.execute('SELECT 1 FROM assignment WHERE event_id=? AND job=?',(assignment['replace_of'],job)).fetchone():
            state,reason,worker,assignment_id='blocked','replacement does not cite existing assignment',None,None
        elif not cert or cert['issuer']!='agency':
            state,reason,worker,assignment_id='blocked','worker certificate does not cover task and entire window',None,None
        else:
            clash=None
            for peer in db.execute('SELECT * FROM assignment WHERE worker=? AND job<>? ORDER BY at,event_id',(assignment['worker'],job)):
                peer_contract,peer_top,peer_condition=effective(db,peer['job'])
                if not peer_contract or peer_condition!='valid' or not peer_top or peer['client_approved']!=peer_top['client']:
                    continue
                if peer_contract['start']<contract['end'] and peer_contract['end']>contract['start'] and (peer['at'],peer['event_id'])<(assignment['at'],assignment['event_id']):
                    clash=peer
                    break
            if clash:state,reason,worker,assignment_id='waitlist',f'worker already committed to {clash["job"]}',None,None
            else:state,reason,worker,assignment_id='assigned','qualified worker reserved for client window',assignment['worker'],assignment['event_id']
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
        for row in data.get('assignments',[]):
            if record(db,row['event_id'],'assignment',row):
                db.execute('INSERT INTO assignment VALUES(?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['worker'],row['at'],row['client_approved'],row.get('replace_of')))
            touched.add(row['job'])
        touched.update(r[0] for r in db.execute('SELECT DISTINCT job FROM assignment'))
        return [decide(db,job) for job in sorted(touched)]
