import json

from .portal import case_status, shared_cases


def _chain(db, claim_id, bindings):
    first = {r['external_case']: r['t'] for r in db.execute(
        'SELECT external_case,MIN(received_at) t FROM portal_receipt WHERE claim_id=? GROUP BY external_case', (claim_id,))}
    bound = {b['external_case']: b for b in bindings}
    shared = shared_cases(db)
    chain = []
    for ext in set(first) | set(bound):
        binding = bound.get(ext)
        if ext in shared:
            status = 'disputed'
        elif binding and binding['state'] in ('withdrawn', 'disputed'):
            status = binding['state']
        else:
            status = case_status(db, claim_id, ext) or 'submitted'
        chain.append({'external_case': ext, 'status': status, 'first_seen': first.get(ext, '')})
    chain.sort(key=lambda c: (c['first_seen'] or '9999', c['external_case']))
    return chain


def _insurance(db):
    claims = []
    for case in db.execute('SELECT * FROM insurance_case ORDER BY claim_id'):
        cid = case['claim_id']
        bindings = [dict(r) for r in db.execute(
            'SELECT external_case,notice_event,operation,amount,state FROM case_binding WHERE claim_id=? ORDER BY operation', (cid,))]
        ops = [dict(r) for r in db.execute(
            'SELECT operation,notice_event,kind,amount,target_case,external_case,state,digest FROM submit_intent WHERE claim_id=? ORDER BY operation', (cid,))]
        live = {b['external_case'] for b in bindings if b['state'] == 'active'}
        recoverable = sum(o['amount'] for o in ops
                          if o['kind'] in ('submit', 'append') and o['state'] in ('bound', 'acknowledged') and o['external_case'] in live)
        pending = sum(o['amount'] for o in ops
                      if o['kind'] in ('submit', 'append') and o['state'] == 'bound' and o['external_case'] in live)
        declared = db.execute('SELECT claimed_amount FROM insurance_notice WHERE claim_id=? ORDER BY event_id DESC LIMIT 1', (cid,)).fetchone()
        claims.append({'claim_id': cid, 'state': case['state'], 'reason': case['reason'], 'external_case': case['external_case'],
                       'declared': declared['claimed_amount'] if declared else None,
                       'recoverable': recoverable, 'pending_review': pending,
                       'pending_queries': [o['operation'] for o in ops if o['state'] == 'unknown'],
                       'case_chain': _chain(db, cid, bindings), 'bindings': bindings, 'operations': ops,
                       'exchanges': [dict(r) for r in db.execute(
                           'SELECT operation,action,outcome,external_case FROM portal_exchange WHERE operation IN '
                           '(SELECT operation FROM submit_intent WHERE claim_id=?) ORDER BY id', (cid,))]})
    disputes = []
    for ext in sorted(shared_cases(db)):
        involved = sorted({r['claim_id'] for r in db.execute(
            'SELECT claim_id FROM portal_receipt WHERE external_case=? UNION SELECT claim_id FROM case_binding WHERE external_case=?',
            (ext, ext))})
        disputes.append({'external_case': ext, 'claims': involved})
    return {'claims': claims, 'disputes': disputes}


def report(db):
    jobs=[]
    for row in db.execute('SELECT DISTINCT job FROM service_contract ORDER BY job'):
        job=row['job'];item={'job':job}
        for table,key in [('contract_decision','contract'),('dispatch_decision','dispatch'),('settlement_decision','settlement')]:
            found=db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone()
            item[key]=dict(found) if found else None
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE job=? ORDER BY id',(job,))]
        jobs.append(item)
    return {'jobs':jobs,'insurance_cases':[dict(r) for r in db.execute('SELECT * FROM insurance_case ORDER BY claim_id')],
            'insurance':_insurance(db),
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}

def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
