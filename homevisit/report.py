import json

def report(db):
    jobs=[]
    for row in db.execute('SELECT DISTINCT job FROM service_contract ORDER BY job'):
        job=row['job'];item={'job':job}
        for table,key in [('contract_decision','contract'),('dispatch_decision','dispatch'),('settlement_decision','settlement')]:
            found=db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone()
            item[key]=dict(found) if found else None
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE job=? ORDER BY id',(job,))]
        jobs.append(item)
    seats=[]
    for row in db.execute('SELECT * FROM seat_decision ORDER BY job'):
        item={'seat':row['job'],'state':row['state'],'reason':row['reason'],'version':row['version']}
        for key in ('confirmed','proposed','waitlist','pending','manual'):
            item[key]=json.loads(row[key])
        item['notices']=[dict(n) for n in db.execute('SELECT kind,applicant,detail FROM seat_notice WHERE seat=? ORDER BY kind,applicant',(row['job'],))]
        seats.append(item)
    return {'jobs':jobs,'applicant_cases':[dict(r) for r in db.execute('SELECT * FROM applicant_case ORDER BY sender')],
            'interview_seats':seats,
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}

def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
