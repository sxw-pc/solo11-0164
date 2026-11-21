"""Offline RPA intake of synthetic job-application EML messages."""

from email.parser import BytesParser
from email.policy import default
from .source import record


def import_data(db, data):
    senders = set()
    with db:
        for row in data.get('messages', []):
            parsed = BytesParser(policy=default).parsebytes(row['eml'].encode('utf8'))
            sender = str(parsed['From'])
            subject = str(parsed['Subject'])
            body = parsed.get_body(preferencelist=('plain',))
            value = body.get_content().strip() if body else ''
            if record(db, row['event_id'], 'applicant_mail', row):
                db.execute('INSERT INTO applicant_mail VALUES(?,?,?,?,?,?)',
                           (row['event_id'], str(parsed['Message-ID']), sender, row['received_at'], subject, value))
            senders.add(sender)
        from .recruitment import decide
        return [decide(db, sender) for sender in sorted(senders)]
