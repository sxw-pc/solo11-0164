"""Offline intake of synthetic customer data-access requests."""

from .source import record


def import_data(db, data):
    touched = []
    with db:
        for row in data.get('requests', []):
            if record(db, row['event_id'], 'data_request', row):
                db.execute('INSERT INTO data_request VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['requester'], row['job'], row['recipient'], row['scope'], row['signed_by']))
            touched.append(row['event_id'])
        from .export import decide
        return [decide(db, request_id) for request_id in sorted(set(touched))]
