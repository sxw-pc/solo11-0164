"""Insurance case state from synthetic insurer portal receipts and RPA bindings.

Receipts may arrive before the notice file, be scraped twice, or reach the
ledger after a correction receipt. A withdrawal receipt is terminal: a
late-arriving old acceptance never reopens the case. An external case number
referenced by two different customer claims is a dispute, never a silent merge.
"""

from .source import record

ACK = ('accepted', 'submitted', 'under_review', 'paid')


def case_status(db, claim_id, external_case):
    """Effective portal status of one external case; withdrawal is terminal."""
    receipts = db.execute(
        'SELECT * FROM portal_receipt WHERE claim_id=? AND external_case=? ORDER BY received_at,event_id',
        (claim_id, external_case)).fetchall()
    if any(r['status'] == 'withdrawn' for r in receipts):
        return 'withdrawn'
    if receipts:
        return receipts[-1]['status']
    return None


def shared_cases(db):
    """External case numbers referenced by more than one customer claim."""
    rows = db.execute(
        'SELECT external_case FROM ('
        'SELECT external_case,claim_id FROM case_binding '
        'UNION SELECT external_case,claim_id FROM portal_receipt'
        ') GROUP BY external_case HAVING COUNT(DISTINCT claim_id)>1').fetchall()
    return {r['external_case'] for r in rows}


def _sync(db, claim_id):
    """Fold portal receipts into RPA intent and binding state for one claim."""
    for r in db.execute('SELECT * FROM portal_receipt WHERE claim_id=? ORDER BY received_at,event_id', (claim_id,)).fetchall():
        if r['status'] == 'withdrawn':
            db.execute("UPDATE case_binding SET state='withdrawn' WHERE external_case=? AND claim_id=? AND state='active'",
                       (r['external_case'], claim_id))
            db.execute("UPDATE submit_intent SET state='done' WHERE claim_id=? AND kind='withdraw' AND target_case=? AND state='bound'",
                       (claim_id, r['external_case']))
        elif r['status'] in ACK and r['operation']:
            db.execute("UPDATE submit_intent SET state='acknowledged' WHERE operation=? AND claim_id=? AND state='bound'",
                       (r['operation'], claim_id))
    for case in shared_cases(db):
        db.execute("UPDATE case_binding SET state='disputed' WHERE external_case=? AND state='active'", (case,))


def decide(db, claim_id):
    _sync(db, claim_id)
    notice = db.execute('SELECT * FROM insurance_notice WHERE claim_id=? ORDER BY event_id LIMIT 1', (claim_id,)).fetchone()
    receipts = db.execute('SELECT * FROM portal_receipt WHERE claim_id=? ORDER BY received_at,event_id', (claim_id,)).fetchall()
    bindings = db.execute('SELECT * FROM case_binding WHERE claim_id=? ORDER BY operation', (claim_id,)).fetchall()
    mine = {b['external_case'] for b in bindings} | {r['external_case'] for r in receipts}
    disputed = sorted(shared_cases(db) & mine)
    if disputed:
        state, reason, external = 'disputed', f'external case {disputed[0]} referenced by multiple claims', disputed[0]
    elif not notice:
        state, reason, external = 'pending', 'notice missing', None
    elif bindings:
        active = [b for b in bindings if b['state'] == 'active']
        if active:
            current = active[-1]
            state = case_status(db, claim_id, current['external_case']) or 'submitted'
            reason = f"portal case bound via operation {current['operation']}"
            external = current['external_case']
        else:
            state, reason, external = 'withdrawn', 'all bound portal cases withdrawn', bindings[-1]['external_case']
    elif receipts:
        current = None
        for r in receipts:
            if case_status(db, claim_id, r['external_case']) != 'withdrawn':
                current = r
        if current:
            state, reason, external = current['status'], 'portal receipt recorded', current['external_case']
        else:
            state, reason, external = 'withdrawn', 'portal case withdrawn', receipts[-1]['external_case']
    else:
        state, reason, external = 'ready', 'notice recorded', None
    db.execute('INSERT INTO insurance_case VALUES(?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,external_case=excluded.external_case',
               (claim_id, state, reason, external))
    return dict(db.execute('SELECT * FROM insurance_case WHERE claim_id=?', (claim_id,)).fetchone())


def refresh_claims(db, claim_ids):
    """Re-decide the given claims plus every claim sharing an external case number."""
    affected = set(claim_ids)
    for cid in list(claim_ids):
        cases = {r['external_case'] for r in db.execute('SELECT external_case FROM portal_receipt WHERE claim_id=?', (cid,))}
        cases |= {r['external_case'] for r in db.execute('SELECT external_case FROM case_binding WHERE claim_id=?', (cid,))}
        for case in cases:
            affected.update(r['claim_id'] for r in db.execute('SELECT DISTINCT claim_id FROM portal_receipt WHERE external_case=?', (case,)))
            affected.update(r['claim_id'] for r in db.execute('SELECT DISTINCT claim_id FROM case_binding WHERE external_case=?', (case,)))
    return [decide(db, claim) for claim in sorted(affected)]


def import_data(db, data):
    claims = set()
    with db:
        for row in data.get('receipts', []):
            if record(db, row['event_id'], 'portal_receipt', row):
                dup = db.execute('SELECT 1 FROM portal_receipt WHERE claim_id=? AND external_case=? AND status=? AND received_at=?',
                                 (row['claim_id'], row['external_case'], row['status'], row['received_at'])).fetchone()
                if not dup:
                    db.execute('INSERT INTO portal_receipt(event_id,claim_id,external_case,status,received_at,operation) VALUES(?,?,?,?,?,?)',
                               (row['event_id'], row['claim_id'], row['external_case'], row['status'], row['received_at'], row.get('operation')))
            claims.add(row['claim_id'])
        return refresh_claims(db, claims)
