"""Baseline case state from synthetic insurer portal receipts."""

from .source import record


def decide(db, claim_id):
    notice = db.execute('SELECT * FROM insurance_notice WHERE claim_id=? ORDER BY event_id LIMIT 1', (claim_id,)).fetchone()
    receipts = db.execute('SELECT * FROM portal_receipt WHERE claim_id=? ORDER BY received_at DESC,event_id', (claim_id,)).fetchall()
    if not notice:
        state, reason, external = 'pending', 'notice missing', None
    elif not receipts:
        state, reason, external = 'ready', 'notice recorded', None
    else:
        state, reason, external = receipts[0]['status'], 'portal receipt recorded', receipts[0]['external_case']
    db.execute('INSERT INTO insurance_case VALUES(?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,external_case=excluded.external_case',
               (claim_id, state, reason, external))
    return dict(db.execute('SELECT * FROM insurance_case WHERE claim_id=?', (claim_id,)).fetchone())


def import_data(db, data):
    claims = set()
    with db:
        for row in data.get('receipts', []):
            if record(db, row['event_id'], 'portal_receipt', row):
                db.execute('INSERT INTO portal_receipt VALUES(?,?,?,?,?)',
                           (row['event_id'], row['claim_id'], row['external_case'], row['status'], row['received_at']))
            claims.add(row['claim_id'])
        return [decide(db, claim) for claim in sorted(claims)]
