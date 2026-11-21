"""Offline intake of synthetic property-damage insurance notices."""

from .source import record


def import_data(db, data):
    claims = set()
    with db:
        for row in data.get('notices', []):
            if record(db, row['event_id'], 'insurance_notice', row):
                db.execute('INSERT INTO insurance_notice VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['claim_id'], row['job'], row['damage'], row['claimed_amount'], row['client_signer']))
            claims.add(row['claim_id'])
        from .portal import decide
        return [decide(db, claim) for claim in sorted(claims)]
