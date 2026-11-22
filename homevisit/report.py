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
    deliveries=[dict(r) for r in db.execute('SELECT request_id,version,recipient,job,state,reason,sha,superseded FROM export_delivery ORDER BY request_id,version')]
    reissue=[dict(r) for r in db.execute('''SELECT d.request_id AS request_id,d.recipient AS recipient,d.version AS version
        FROM export_delivery d WHERE d.superseded=0
        AND EXISTS(SELECT 1 FROM export_delivery o WHERE o.request_id=d.request_id AND o.superseded=1) ORDER BY d.request_id''')]
    return {'jobs':jobs,'export_packages':[dict(r) for r in db.execute('SELECT * FROM export_package ORDER BY request_id')],
            'export_deliveries':deliveries,'reissue_required':reissue,
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}

def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
