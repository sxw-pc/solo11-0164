"""Baseline applicant queue from collected synthetic messages."""


def decide(db, sender):
    rows = db.execute('SELECT * FROM applicant_mail WHERE sender=? ORDER BY received_at DESC,event_id', (sender,)).fetchall()
    state = 'review' if any('apply' in row['subject'].lower() for row in rows) else 'logged'
    subject = rows[0]['subject'] if rows else ''
    db.execute('INSERT INTO applicant_case VALUES(?,?,?,?) ON CONFLICT(sender) DO UPDATE SET state=excluded.state,message_count=excluded.message_count,latest_subject=excluded.latest_subject',
               (sender, state, len(rows), subject))
    return dict(db.execute('SELECT * FROM applicant_case WHERE sender=?', (sender,)).fetchone())
