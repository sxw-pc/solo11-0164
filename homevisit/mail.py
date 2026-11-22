"""Offline RPA intake of synthetic job-application and interview EML messages."""

import re
from email.parser import BytesParser
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime
from .source import record

TOKEN = re.compile(r'INV-[A-Z0-9]+(?:-[A-Z0-9]+)*')
FIELD = re.compile(r'^[A-Za-z][A-Za-z0-9-]*$')


def mailbox(value):
    return parseaddr(str(value or ''))[1].lower()


def own_text(body):
    """Own words of a message, without quoted lines or forwarded originals."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('>'):
            continue
        low = stripped.lower()
        if 'forwarded message' in low or 'begin forwarded' in low or '转发的邮件' in stripped:
            break
        if re.match(r'(?i)^on .+ wrote:$', stripped):
            continue
        lines.append(line)
    return '\n'.join(lines)


def header_fields(text):
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep and FIELD.match(key.strip()):
            fields[key.strip().lower()] = value.strip()
    return fields


def sent_at(parsed, fallback):
    if parsed['Date']:
        try:
            return parsedate_to_datetime(str(parsed['Date'])).isoformat()
        except (TypeError, ValueError):
            pass
    return fallback


def intent_of(own, forwarded):
    low = own.lower()
    if '改期' in own or 'reschedule' in low:
        return 'reschedule'
    if '无法' in own or '不能' in own or 'decline' in low or 'cannot' in low or "can't" in low:
        return 'decline'
    if '参加' in own or 'accept' in low or 'confirm' in low or 'attend' in low:
        return 'accept'
    return 'quote' if forwarded else 'other'


def _notice(db, row, kind, message_id, in_reply_to, sender, when, invitation, fields, target=None):
    db.execute('INSERT INTO interview_notice VALUES(?,?,?,?,?,?,?,?,?)',
               (row['event_id'], kind, message_id, in_reply_to, sender, when, invitation, target,
                int(bool(fields.get('signed')))))


def classify(db, row, parsed, subject, body):
    sender = str(parsed['From'])
    message_id = str(parsed['Message-ID'])
    when = sent_at(parsed, row['received_at'])
    in_reply_to = str(parsed['In-Reply-To']) if parsed['In-Reply-To'] else None
    own = own_text(body)
    fields = header_fields(own)
    forwarded = (bool(re.match(r'(?i)\s*(fwd?|转发)', subject))
                 or 'forwarded message' in body.lower() or '转发的邮件' in body)
    token = TOKEN.search(body)
    if 'mailer-daemon' in sender.lower() or subject.lower().startswith('delivery status notification'):
        _notice(db, row, 'bounce', message_id, in_reply_to, sender, when, None, fields,
                fields.get('original-message-id'))
    elif {'invitation', 'slot', 'reply-by'} <= set(fields):
        slot = fields['slot'].split('/')
        seat = '|'.join((fields.get('position', ''), slot[0], slot[-1], fields.get('channel', '')))
        values = (fields['invitation'], row['event_id'], message_id, sender, str(parsed['To']),
                  fields.get('position', ''), slot[0], slot[-1], fields.get('timezone', ''),
                  fields.get('channel', ''), int(fields.get('seats', '1')), fields['reply-by'],
                  fields.get('policy', 'first-declared'), when, fields.get('signed', ''), seat)
        old = db.execute('SELECT * FROM interview_invite WHERE invitation=?', (values[0],)).fetchone()
        if old:
            if tuple(old) != values:
                raise ValueError(f'invitation conflict: {values[0]}')
        else:
            db.execute('INSERT INTO interview_invite VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
    elif 'confirmation-for' in fields:
        _notice(db, row, 'confirm', message_id, in_reply_to, sender, when, fields['confirmation-for'], fields)
    elif 'cancel-receipt' in fields:
        _notice(db, row, 'cancel-receipt', message_id, in_reply_to, sender, when, fields['cancel-receipt'], fields)
    elif 'cancel' in fields:
        _notice(db, row, 'cancel', message_id, in_reply_to, sender, when, fields['cancel'], fields)
    elif in_reply_to or token:
        db.execute('INSERT INTO interview_reply VALUES(?,?,?,?,?,?,?,?)',
                   (row['event_id'], message_id, in_reply_to, sender, when,
                    token.group(0) if token else None, intent_of(own, forwarded), int(forwarded)))


def import_data(db, data):
    senders = set()
    with db:
        for row in data.get('messages', []):
            parsed = BytesParser(policy=default).parsebytes(row['eml'].encode('utf8'))
            sender = str(parsed['From'])
            subject = str(parsed['Subject'])
            part = parsed.get_body(preferencelist=('plain',))
            value = part.get_content().strip() if part else ''
            if record(db, row['event_id'], 'applicant_mail', row):
                db.execute('INSERT INTO applicant_mail VALUES(?,?,?,?,?,?)',
                           (row['event_id'], str(parsed['Message-ID']), sender, row['received_at'], subject, value))
                classify(db, row, parsed, subject, value)
            senders.add(sender)
        from .recruitment import decide
        cases = [decide(db, sender) for sender in sorted(senders)]
        from .interview import decide_all
        return [*cases, *decide_all(db)]
