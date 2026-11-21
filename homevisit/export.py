"""Baseline package readiness for customer data requests."""


def decide(db, request_id):
    request = db.execute('SELECT * FROM data_request WHERE event_id=?', (request_id,)).fetchone()
    if not request:
        state, reason, count = 'pending', 'request missing', 0
    elif request['signed_by'] != request['requester']:
        state, reason, count = 'review', 'requester signature missing', 0
    else:
        state, reason, count = 'ready', 'signed request recorded', 1
    db.execute('INSERT INTO export_package VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,record_count=excluded.record_count',
               (request_id, state, reason, count))
    return dict(db.execute('SELECT * FROM export_package WHERE request_id=?', (request_id,)).fetchone())
