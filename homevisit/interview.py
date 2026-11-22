"""Interview seat commitments derived from signed recruiter mail.

Allocation never uses import order: commitments and waitlist ranks come from
the declared send time of each message, the trust of its source (recruiter
signature, exact invitee mailbox) and the invitation policy. A confirmation
letter already sent is never silently withdrawn; conflicts raise compensation
notices and wait for manual review.
"""

import json
from email.utils import parseaddr
from .store import update

RESOURCE = {'onsite': 'meeting-room', 'remote': 'video-link'}
ACTIVE = ('confirmed', 'confirm_bounced', 'cancelling')


def mailbox(value):
    return parseaddr(str(value or ''))[1].lower()


def decide_all(db):
    seats = [r[0] for r in db.execute('SELECT DISTINCT seat FROM interview_invite ORDER BY seat')]
    return [decide_seat(db, seat) for seat in seats]


def _invitation_status(inv, replies, notices, bounces, now, case_state):
    """Mail-driven status of one invitation."""
    personal = [r for r in replies if r['intent'] in ('accept', 'decline', 'reschedule')
                and mailbox(r['sender']) == mailbox(inv['recipient'])]
    untrusted = [r for r in replies if r not in personal]
    confirms = [n for n in notices if n['kind'] == 'confirm' and n['signed']
                and mailbox(n['sender']) == mailbox(inv['sender'])]
    bad_confirms = [n for n in notices if n['kind'] == 'confirm' and n not in confirms]
    cancels = [n for n in notices if n['kind'] == 'cancel'
               and (mailbox(n['sender']) == mailbox(inv['recipient'])
                    or (n['signed'] and mailbox(n['sender']) == mailbox(inv['sender'])))]
    bad_cancels = [n for n in notices if n['kind'] == 'cancel' and n not in cancels]
    parties = {mailbox(inv['sender']), mailbox(inv['recipient'])}
    receipts = [n for n in notices if n['kind'] == 'cancel-receipt' and mailbox(n['sender']) in parties]
    confirm_ids = {c['message_id'] for c in confirms}
    invite_bounced = any(b['target_message'] == inv['message_id'] for b in bounces)
    confirm_bounced = any(b['target_message'] in confirm_ids for b in bounces)
    latest = personal[-1] if personal else None
    accepted = bool(latest and latest['intent'] == 'accept')
    late = bool(accepted and latest['sent_at'] > inv['reply_by'])
    declined = bool(latest and latest['intent'] == 'decline')
    cancel_requested = bool(cancels) or case_state == 'withdrawn' or (declined and bool(confirms))
    if cancel_requested and confirms and not receipts:
        status = 'cancelling'
    elif cancel_requested:
        status = 'cancelled'
    elif confirms:
        status = 'confirm_bounced' if confirm_bounced else 'confirmed'
    elif latest is None:
        if invite_bounced:
            status = 'undelivered'
        elif inv['reply_by'] < now:
            status = 'expired'
        else:
            status = 'pending'
    elif accepted:
        status = 'accepted'
    elif latest['intent'] == 'reschedule':
        status = 'reschedule'
    else:
        status = 'released'
    return {'status': status, 'late': late, 'latest': latest, 'accepted': accepted,
            'untrusted': untrusted, 'bad_confirms': bad_confirms, 'bad_cancels': bad_cancels,
            'invite_bounced': invite_bounced, 'confirm_bounced': confirm_bounced}


def decide_seat(db, seat):
    invites_all = db.execute('SELECT * FROM interview_invite ORDER BY invitation').fetchall()
    replies_all = db.execute('SELECT * FROM interview_reply ORDER BY sent_at,event_id').fetchall()
    notices_all = db.execute('SELECT * FROM interview_notice ORDER BY sent_at,event_id').fetchall()
    cases = {mailbox(r['sender']): r['state'] for r in db.execute('SELECT sender,state FROM applicant_case')}
    now = db.execute('SELECT MAX(received_at) FROM applicant_mail').fetchone()[0] or ''
    by_message = {i['message_id']: i['invitation'] for i in invites_all}
    reply_map = {i['invitation']: [] for i in invites_all}
    for reply in replies_all:
        keys = {reply['invitation'], by_message.get(reply['in_reply_to'])} - {None}
        for key in keys:
            if key in reply_map and all(r['event_id'] != reply['event_id'] for r in reply_map[key]):
                reply_map[key].append(reply)
    notice_map = {i['invitation']: [] for i in invites_all}
    for note in notices_all:
        if note['invitation'] in notice_map:
            notice_map[note['invitation']].append(note)
    bounces = [n for n in notices_all if n['kind'] == 'bounce']
    cache = {}

    def status_of(inv):
        key = inv['invitation']
        if key not in cache:
            cache[key] = _invitation_status(inv, reply_map.get(key, []), notice_map.get(key, []),
                                            bounces, now, cases.get(mailbox(inv['recipient']), 'logged'))
        return cache[key]

    invites = [i for i in invites_all if i['seat'] == seat]
    manual, seat_notices = [], []
    capacities = {i['capacity'] for i in invites}
    capacity = min(capacities)
    if len(capacities) > 1:
        manual.append({'issue': 'capacity-conflict', 'detail': 'invitations disagree on seat capacity'})
    if len({i['reply_by'] for i in invites}) > 1:
        manual.append({'issue': 'deadline-conflict', 'detail': 'invitations disagree on reply deadline'})
    for inv in invites:
        if not inv['signed_by']:
            manual.append({'issue': 'unsigned-invitation', 'invitation': inv['invitation'],
                           'detail': 'invitation lacks recruiter signature'})

    def overlaps(this, other):
        return this['slot_start'] < other['slot_end'] and other['slot_start'] < this['slot_end']

    # Cross-seat: an applicant confirmed for an overlapping slot cannot take
    # this seat; their acceptance here is superseded (never deleted).
    for inv in invites:
        applicant = mailbox(inv['recipient'])
        for other in invites_all:
            if other['seat'] == seat or mailbox(other['recipient']) != applicant or not overlaps(inv, other):
                continue
            if status_of(other)['status'] not in ('confirmed', 'confirm_bounced'):
                continue
            own = status_of(inv)
            if own['status'] == 'accepted':
                own['status'] = 'superseded'
                own['detail'] = f"applicant confirmed in overlapping seat {other['seat']}"
            elif own['status'] in ('confirmed', 'confirm_bounced'):
                manual.append({'issue': 'double-booked', 'applicant': applicant,
                               'detail': f"confirmed in overlapping seats {seat} and {other['seat']}"})

    confirmed, pool, pending = [], [], []
    for inv in invites:
        info = status_of(inv)
        status = info['status']
        applicant = mailbox(inv['recipient'])
        entry = {'invitation': inv['invitation'], 'applicant': applicant, 'channel': inv['channel']}
        if status in ACTIVE:
            confirmed.append({**entry, 'resource': RESOURCE.get(inv['channel'], inv['channel']),
                              'delivery': 'failed' if status == 'confirm_bounced' else 'sent',
                              'status': status})
            if status == 'confirm_bounced':
                manual.append({'issue': 'confirmation-bounced', 'invitation': inv['invitation'],
                               'applicant': applicant,
                               'detail': 'confirmation letter bounced; commitment kept, not silently withdrawn'})
                seat_notices.append({'kind': 'compensation', 'applicant': applicant,
                                     'detail': 'confirmation delivery failed; resend and compensate pending manual review'})
            if status == 'cancelling':
                manual.append({'issue': 'cancel-pending-receipt', 'invitation': inv['invitation'],
                               'applicant': applicant,
                               'detail': 'confirmed seat cancellation awaits receipt'})
        elif status == 'accepted':
            pool.append((inv, info))
        elif status == 'pending':
            pending.append({**entry, 'reply_by': inv['reply_by']})
        elif status == 'undelivered':
            manual.append({'issue': 'invitation-undelivered', 'invitation': inv['invitation'],
                           'applicant': applicant,
                           'detail': 'invitation bounced without reply; resend via another channel'})
        elif status == 'reschedule':
            manual.append({'issue': 'reschedule-requested', 'invitation': inv['invitation'],
                           'applicant': applicant, 'detail': 'applicant asked to reschedule'})
        for reply in info['untrusted']:
            manual.append({'issue': 'untrusted-reply', 'invitation': inv['invitation'], 'sender': reply['sender'],
                           'detail': 'forwarded quote or non-invitee mailbox is not a personal confirmation'})
        if info['bad_confirms']:
            manual.append({'issue': 'untrusted-confirmation', 'invitation': inv['invitation'],
                           'detail': 'confirmation lacks recruiter signature or sender'})
        if info['bad_cancels']:
            manual.append({'issue': 'untrusted-cancel', 'invitation': inv['invitation'],
                           'detail': 'cancellation from unauthorized sender'})

    # Rank acceptances by declared send time, lateness and policy; import
    # order never grabs a seat.
    ranked = sorted(pool, key=lambda t: (t[1]['late'], t[1]['latest']['sent_at'], t[0]['invitation']))
    open_slots = max(0, capacity - len(confirmed))
    proposed = [{'invitation': inv['invitation'], 'applicant': mailbox(inv['recipient']),
                 'sent_at': info['latest']['sent_at'], 'late': info['late']} for inv, info in ranked[:open_slots]]
    waitlist = [{'invitation': inv['invitation'], 'applicant': mailbox(inv['recipient']),
                 'sent_at': info['latest']['sent_at'], 'late': info['late'],
                 'rank': open_slots + n + 1} for n, (inv, info) in enumerate(ranked[open_slots:])]

    # A confirmation letter already sent stands, but if policy now ranks an
    # unconfirmed acceptance ahead of it, record compensation and ask humans.
    confirmed_ids = {c['invitation'] for c in confirmed}
    for inv in invites:
        if inv['invitation'] not in confirmed_ids:
            continue
        info = status_of(inv)
        if not info['accepted']:
            manual.append({'issue': 'confirmation-without-acceptance', 'invitation': inv['invitation'],
                           'detail': 'confirmation letter without personal acceptance'})
            continue
        own_key = (info['late'], info['latest']['sent_at'], inv['invitation'])
        for other, other_info in ranked:
            other_key = (other_info['late'], other_info['latest']['sent_at'], other['invitation'])
            if other_key < own_key:
                manual.append({'issue': 'confirmation-overrides-policy', 'invitation': inv['invitation'],
                               'detail': f"kept confirmation outranked by {other['invitation']}; not silently withdrawn"})
                seat_notices.append({'kind': 'compensation', 'applicant': mailbox(other['recipient']),
                                     'detail': 'seat kept by earlier confirmation letter; compensate pending manual review'})
                break

    unique = []
    for item in manual:
        if item not in unique:
            unique.append(item)
    manual = unique

    if manual:
        state, reason = 'manual_review', '; '.join(sorted({m['issue'] for m in manual}))
    elif confirmed and len(confirmed) >= capacity:
        state, reason = 'committed', 'all seats covered by signed confirmation letters'
    elif confirmed:
        state, reason = 'partial', 'some seats confirmed, capacity remains'
    elif pending or proposed:
        state, reason = 'open', 'awaiting applicant replies or recruiter confirmation letters'
    else:
        state, reason = 'closed', 'no active invitations remain'

    db.execute('DELETE FROM seat_notice WHERE seat=?', (seat,))
    for note in seat_notices:
        db.execute('INSERT OR REPLACE INTO seat_notice VALUES(?,?,?,?)',
                   (seat, note['kind'], note['applicant'], note['detail']))
    dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
    return update(db, 'seat_decision', seat, state=state, reason=reason,
                  confirmed=dump(confirmed), proposed=dump(proposed), waitlist=dump(waitlist),
                  pending=dump(pending), manual=dump(manual))
