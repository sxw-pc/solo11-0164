"""Baseline applicant queue from collected synthetic messages."""

WITHDRAW = ('withdraw', '撤回')


def decide(db, sender):
    rows = db.execute('SELECT * FROM applicant_mail WHERE sender=? ORDER BY received_at DESC,event_id', (sender,)).fetchall()
    state = 'logged'
    for row in rows:
        subject = row['subject'].lower()
        if any(key in subject for key in WITHDRAW):
            state = 'withdrawn'
            break
        if 'apply' in subject:
            state = 'review'
            break
    subject = rows[0]['subject'] if rows else ''
    db.execute('INSERT INTO applicant_case VALUES(?,?,?,?) ON CONFLICT(sender) DO UPDATE SET state=excluded.state,message_count=excluded.message_count,latest_subject=excluded.latest_subject',
               (sender, state, len(rows), subject))
    return dict(db.execute('SELECT * FROM applicant_case WHERE sender=?', (sender,)).fetchone())
