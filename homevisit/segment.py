"""Segmented relay-dispatch and split-performance adjudication.

A single service job may be carried by two similarly qualified workers who
relieve each other inside the contracted window.  The evidence chain is:

relay request (client authorized) -> two-party signed handover receipt
-> independent start/end receipts per worker -> per-worker certificates
-> client acceptances / disputes attributed to concrete service fragments
-> final client responsibility confirmation selecting one settlement route.

Two candidate routes ("lead": the first worker stays responsible for the
whole window, "relay": responsibility passes at the handoff point) are always
adjudicated from the *same* evidence.  Only the client's final responsibility
confirmation marks one route effective; the other route and its evidence are
retained.  This module never invents receipts or signatures.
"""
from datetime import datetime

from .contract import effective
from .store import update, rebuild_segments

ROUTE_LEAD = 'lead'
ROUTE_RELAY = 'relay'
ROUTE_SINGLE = 'single'


def parse(value):
    return datetime.fromisoformat(value) if value else None


def minutes(a, b):
    return int((b - a).total_seconds() // 60)


def certificate(db, worker, task, start, end):
    """Return (cert_row, revoke_row) covering [start, end] for the task.

    A late-recorded revocation whose effective time is at or before the end of
    the fragment means the certificate cannot cover that fragment; a prior
    worker's certificate never covers a successor.
    """
    cert = db.execute(
        'SELECT * FROM worker_certificate WHERE worker=? AND task=? AND valid_from<=? AND valid_to>=? '
        'ORDER BY valid_to DESC,event_id',
        (worker, task, start.isoformat(), end.isoformat())).fetchone()
    if not cert or cert['issuer'] != 'agency':
        return None, None
    rev = db.execute(
        'SELECT * FROM cert_revocation WHERE worker=? AND task=? AND at<=? ORDER BY at DESC,event_id',
        (worker, task, end.isoformat())).fetchone()
    return cert, rev


def _pause_intervals(rows):
    pauses = sorted((r for r in rows if r['kind'] == 'pause'), key=lambda r: (r['at'], r['event_id']))
    resumes = sorted((r for r in rows if r['kind'] == 'resume'), key=lambda r: (r['at'], r['event_id']))
    if len(pauses) != len(resumes):
        return None, 'unmatched pause or resume receipt'
    intervals = []
    for a, b in zip(pauses, resumes):
        if a['at'] >= b['at']:
            return None, 'unmatched pause or resume receipt'
        intervals.append((parse(a['at']), parse(b['at'])))
    return intervals, None


def _build_spans(receipts, contract, top):
    """Raw on-site presence per worker, validated against contract evidence.

    Each error carries a state hint: documentary problems block the job,
    while on-site evidence problems are disputes (legacy semantics).
    """
    grouped = {}
    for receipt in receipts:
        grouped.setdefault(receipt['worker'], []).append(receipt)
    spans, errors = {}, []
    window_start, window_end = parse(contract['start']), parse(contract['end'])
    for worker, rows in grouped.items():
        starts = sorted((r for r in rows if r['kind'] == 'start'), key=lambda r: (r['at'], r['event_id']))
        ends = sorted((r for r in rows if r['kind'] == 'end'), key=lambda r: (r['at'], r['event_id']))
        if not starts or not ends:
            errors.append(('disputed', f'{worker}: worker lacks start or end receipt'))
            continue
        first, last = starts[0], ends[-1]
        if not (first['client'] == top['client'] == last['client']
                and first['address'] == contract['address'] == last['address']
                and window_start <= parse(first['at']) < parse(last['at']) <= window_end):
            errors.append(('disputed', f'{worker}: worker, client, address or service window mismatch'))
            continue
        pauses, pause_error = _pause_intervals(rows)
        if pause_error:
            errors.append(('disputed', f'{worker}: {pause_error}'))
            continue
        start, end = parse(first['at']), parse(last['at'])
        if any(start > a or b > end for a, b in pauses):
            errors.append(('disputed', f'{worker}: pause outside on-site interval'))
            continue
        paused = sum(minutes(a, b) for a, b in pauses)
        spans[worker] = {'start': start, 'end': end, 'pauses': pauses,
                         'site_minutes': max(0, minutes(start, end) - paused)}
    return spans, errors


def _merge(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def union_site_minutes(spans):
    """On-site minutes across workers, counting a short overlap only once."""
    total = 0
    for a, b in _merge([(s['start'], s['end']) for s in spans.values()]):
        total += minutes(a, b)
    # Subtract pauses, never letting an overlap subtract service time twice.
    for a, b in sorted(p for s in spans.values() for p in s['pauses']):
        total -= minutes(a, b)
    return max(0, total)


def relay_points(db, job):
    """Canonical relay points shared by dispatch conflict checks and billing.

    The point timestamps come solely from relay_request rows; both modules use
    this function so a handoff can never drift between dispatch and settlement.
    """
    requests = db.execute('SELECT * FROM relay_request WHERE job=? ORDER BY at,event_id', (job,)).fetchall()
    handovers = db.execute('SELECT * FROM handover_receipt WHERE job=?', (job,)).fetchall()
    points = []
    for req in requests:
        hand = next((h for h in handovers if h['at'] == req['at']
                     and h['from_worker'] == req['from_worker'] and h['to_worker'] == req['to_worker']), None)
        points.append({'at': parse(req['at']), 'at_text': req['at'], 'from_worker': req['from_worker'],
                       'to_worker': req['to_worker'], 'request_id': req['event_id'],
                       'client_approved': req['client_approved'],
                       'handover_id': hand['event_id'] if hand else None,
                       'from_signed': hand['from_signed'] if hand else None,
                       'to_signed': hand['to_signed'] if hand else None})
    return points


def validate_chain(db, job, contract, top, spans, require_presence):
    """Validate authorization, assignments, handover signatures and presence.

    When require_presence is False (dispatch before receipts arrive) only the
    documentary chain is checked; settlement always requires presence.
    """
    points = relay_points(db, job)
    errors = []
    assignments = db.execute('SELECT * FROM assignment WHERE job=?', (job,)).fetchall()
    by_worker = {}
    for row in assignments:
        by_worker.setdefault(row['worker'], []).append(row)
    chain = []
    previous = None
    window_start, window_end = parse(contract['start']), parse(contract['end'])
    for point in points:
        if point['client_approved'] != top['client']:
            errors.append(('blocked', f"relay {point['request_id']} lacks client authorization"))
        if previous is not None and point['from_worker'] != previous:
            errors.append(('blocked', f"relay {point['request_id']} does not continue the worker chain"))
        previous = point['to_worker']
        chain.extend([point['from_worker'], point['to_worker']])
        if not (window_start <= point['at'] <= window_end):
            errors.append(('blocked', f"handoff {point['at_text']} is outside the current service window"))
        if not point['handover_id']:
            errors.append(('blocked', f"handoff at {point['at_text']} lacks a two-party signed handover receipt"))
        elif not (point['from_signed'] == point['from_worker'] and point['to_signed'] == point['to_worker']):
            errors.append(('blocked', f"handoff at {point['at_text']} is not signed by both relieving workers"))
        if require_presence:
            for worker in (point['from_worker'], point['to_worker']):
                span = spans.get(worker)
                if not span or not (span['start'] <= point['at'] <= span['end']):
                    errors.append(('blocked', f"handoff at {point['at_text']} sits in an unattended gap for {worker}"))
    for worker in dict.fromkeys(chain):
        rows = by_worker.get(worker, [])
        if not any(r['client_approved'] == top['client'] for r in rows):
            errors.append(('blocked', f'relay worker {worker} lacks a client-authorized assignment'))
        for row in rows:
            if row['replace_of'] and not db.execute(
                    'SELECT 1 FROM assignment WHERE event_id=? AND job=?', (row['replace_of'], job)).fetchone():
                errors.append(('blocked', f'assignment {row["event_id"]} cites a missing predecessor'))
    return points, errors


def _overlap_minutes(a, b, intervals):
    total = 0
    for x, y in intervals:
        lo, hi = max(a, x), min(b, y)
        if lo < hi:
            total += minutes(lo, hi)
    return total


def _acceptance_boundaries(acceptances, contract, top):
    boundaries = set()
    for acc in acceptances:
        if acc['task'] != contract['task'] or acc['client'] != top['client']:
            continue
        if acc['win_start']:
            boundaries.add(parse(acc['win_start']))
        if acc['win_end']:
            boundaries.add(parse(acc['win_end']))
    return boundaries


def _matching_acceptances(acceptances, contract, top, worker, start, end):
    accepted, disputed = [], []
    for acc in acceptances:
        if acc['task'] != contract['task'] or acc['client'] != top['client']:
            continue
        if acc['worker'] and acc['worker'] != worker:
            continue
        if acc['win_start'] and acc['win_end']:
            win_start, win_end = parse(acc['win_start']), parse(acc['win_end'])
            if win_end <= start or win_start >= end:
                continue
        if acc['dispute']:
            disputed.append(acc)
        if acc['accepted']:
            accepted.append(acc)
    return accepted, disputed


def _build_segments(db, job, route, pieces, contract, top, acceptances, rate):
    """Turn timeline pieces into contiguous segments, breaking at handoffs and
    at acceptance/dispute window edges so an accepted fragment can still be
    paid while a later disputed fragment is frozen."""
    segments = []
    current = None
    for piece in pieces:
        if current is None or piece['owner'] != current['worker'] or piece['break']:
            if current:
                segments.append(current)
            current = {'route': route, 'idx': len(segments), 'start': piece['start'], 'end': piece['end'],
                       'worker': piece['owner'], 'minutes': 0, 'payable': 0, 'frozen': 0,
                       'state': 'pending', 'reason': 'awaiting adjudication',
                       'relay_at': piece.get('relay_at'), 'assignment_id': None, 'cert_event_id': None,
                       'handover_id': piece.get('handover_id'), 'acceptance_id': None, 'dispute': None}
        current['end'] = piece['end']
        current['minutes'] += piece['billable']
    if current:
        segments.append(current)
    for seg in segments:
        start, end = parse(seg['start']), parse(seg['end'])
        assignment = db.execute(
            'SELECT * FROM assignment WHERE job=? AND worker=? ORDER BY at,event_id',
            (job, seg['worker'])).fetchone()
        cert, rev = certificate(db, seg['worker'], contract['task'], start, end)
        if assignment:
            seg['assignment_id'] = assignment['event_id']
        value = seg['minutes'] * rate // 60
        if not cert:
            seg.update(state='blocked', reason='worker certificate does not cover the responsible fragment',
                       frozen=value, payable=0)
            continue
        seg['cert_event_id'] = cert['event_id']
        if rev:
            seg.update(state='blocked', reason=f"certificate revoked at {rev['at']} for this fragment",
                       frozen=value, payable=0, dispute=f"revocation {rev['event_id']}")
            continue
        accepted, disputed = _matching_acceptances(acceptances, contract, top, seg['worker'], start, end)
        if disputed:
            acc = disputed[0]
            seg.update(state='disputed', reason='client dispute attached to this responsible fragment',
                       frozen=value, payable=0, acceptance_id=acc['event_id'], dispute=acc['dispute'])
        elif accepted:
            acc = accepted[0]
            seg.update(state='settled', reason='client accepted the work of the responsible worker',
                       payable=value, frozen=0, acceptance_id=acc['event_id'])
        else:
            seg.update(state='pending', reason='client has not accepted this responsible fragment',
                       frozen=value, payable=0)
    return segments


def _owner_relay(points, at):
    owner = points[0]['from_worker']
    for point in points:
        if point['at'] <= at:
            owner = point['to_worker']
    return owner


def _route_pieces(route, boundaries, spans, points, acceptance_edges):
    pieces = []
    lead_worker = points[0]['from_worker']
    relay_times = {p['at'] for p in points}
    for a, b in zip(boundaries, boundaries[1:]):
        present = [w for w, s in spans.items() if s['start'] <= a and b <= s['end']]
        if not present:
            continue
        point = next((p for p in points if p['at'] == a), None)
        if route == ROUTE_RELAY:
            owner = _owner_relay(points, a)
            if owner not in present:
                owner = present[0]
            relay_at = point['at_text'] if point else None
        else:
            owner = lead_worker if lead_worker in present else present[0]
            relay_at = None
        # Break at handoff points (relay route) and at acceptance/dispute
        # window edges (every route) so an accepted fragment stays payable
        # while a later disputed fragment is frozen, even on the lead route.
        brk = a in relay_times or a in acceptance_edges
        handover_id = point['handover_id'] if point else None
        # Pauses are charged to whoever is on the piece; overlaps are counted
        # once because the timeline is partitioned by union boundaries.
        billable = max(0, minutes(a, b) - _overlap_minutes(a, b, spans[owner]['pauses']))
        pieces.append({'start': a.isoformat(), 'end': b.isoformat(), 'owner': owner,
                       'billable': billable, 'break': brk, 'relay_at': relay_at,
                       'handover_id': handover_id})
    return pieces


def _blocked_route_segments(db, job, points, spans, rate, reason):
    routes = {}
    plans = {ROUTE_LEAD: [(points[0]['from_worker'], None)],
             ROUTE_RELAY: [(points[0]['from_worker'], None)] + [(p['to_worker'], p['at_text']) for p in points]}
    for route, plan in plans.items():
        segs = []
        for idx, (worker, relay_at) in enumerate(plan):
            span = spans.get(worker)
            seg = {'route': route, 'idx': idx,
                   'start': span['start'].isoformat() if span else None,
                   'end': span['end'].isoformat() if span else None,
                   'worker': worker, 'minutes': span['site_minutes'] if span else 0,
                   'payable': 0, 'frozen': (span['site_minutes'] if span else 0) * rate // 60,
                   'state': 'blocked', 'reason': reason, 'relay_at': relay_at,
                   'assignment_id': None, 'cert_event_id': None, 'handover_id': None,
                   'acceptance_id': None, 'dispute': None}
            assignment = db.execute(
                'SELECT * FROM assignment WHERE job=? AND worker=? ORDER BY at,event_id',
                (job, worker)).fetchone()
            if assignment:
                seg['assignment_id'] = assignment['event_id']
            segs.append(seg)
        routes[route] = segs
    return routes


def adjudicate(db, job):
    """Run segmented adjudication and persist every candidate-route decision."""
    contract, top, condition = effective(db, job)
    receipts = db.execute('SELECT * FROM visit_receipt WHERE job=? ORDER BY at,event_id', (job,)).fetchall()
    acceptances = db.execute('SELECT * FROM task_acceptance WHERE job=? ORDER BY event_id', (job,)).fetchall()
    confirmations = db.execute(
        'SELECT * FROM responsibility_confirmation WHERE job=? ORDER BY at,event_id', (job,)).fetchall()
    rate = top['rate'] if top else 0

    if not contract or condition != 'valid' or not top or top['signed_by'] != top['client']:
        rebuild_segments(db, job, [])
        return update(db, 'settlement_decision', job, state='blocked', reason='service contract or worker dispatch invalid',
                      minutes=0, payable=0, frozen=0,
                      mode='relay' if confirmations else 'single',
                      route=confirmations[-1]['route'] if confirmations else None,
                      coverage_start=None, coverage_end=None, gap_minutes=0)
    dispatch = db.execute('SELECT * FROM dispatch_decision WHERE job=?', (job,)).fetchone()
    has_relay_chain = bool(db.execute('SELECT 1 FROM relay_request WHERE job=?', (job,)).fetchone())
    # Relay jobs keep adjudicating their fragments even when dispatch is
    # blocked: a late certificate revocation freezes only the successor's
    # fragment and must not erase the other worker's accepted work.  Single
    # workers still inherit the legacy hard block.
    if not has_relay_chain and (not dispatch or dispatch['state'] != 'assigned'):
        rebuild_segments(db, job, [])
        return update(db, 'settlement_decision', job, state='blocked', reason='contract or worker dispatch invalid',
                      minutes=0, payable=0, frozen=0, mode='single', route=ROUTE_SINGLE,
                      coverage_start=None, coverage_end=None, gap_minutes=0)

    spans, span_errors = _build_spans(receipts, contract, top)
    points, chain_errors = validate_chain(db, job, contract, top, spans, require_presence=bool(receipts))

    if not points:
        return _adjudicate_single(db, job, contract, top, spans, span_errors, acceptances, rate)

    blocking = span_errors + chain_errors
    presence = _merge([(s['start'], s['end']) for s in spans.values()])
    coverage_start = presence[0][0] if presence else None
    coverage_end = presence[-1][1] if presence else None
    gap_minutes = sum(minutes(b, c) for (_, b), (c, _) in zip(presence, presence[1:])) if presence else 0
    if not blocking and not spans:
        blocking = [('pending', 'independent arrival and departure receipts missing')]
    coverage_minutes = union_site_minutes(spans) if spans else 0

    if blocking:
        reason = '; '.join(message for _, message in blocking)
        level = 'blocked' if any(level_ == 'blocked' for level_, _ in blocking) else 'disputed'
        routes = _blocked_route_segments(db, job, points, spans, rate, reason)
        rebuild_segments(db, job, [seg for route in (ROUTE_RELAY, ROUTE_LEAD) for seg in routes[route]])
        result = update(db, 'settlement_decision', job, state=level, reason=reason,
                        minutes=coverage_minutes, payable=0,
                        frozen=coverage_minutes * rate // 60, mode='relay',
                        route=confirmations[-1]['route'] if confirmations else None,
                        coverage_start=coverage_start.isoformat() if coverage_start else None,
                        coverage_end=coverage_end.isoformat() if coverage_end else None,
                        gap_minutes=gap_minutes)
        result['routes'] = routes
        return result

    acceptance_edges = _acceptance_boundaries(acceptances, contract, top)
    boundaries = sorted({b for s in spans.values() for b in (s['start'], s['end'])}
                        | {p['at'] for p in points}
                        | acceptance_edges)
    boundaries = [t for t in boundaries if coverage_start <= t <= coverage_end]
    routes = {}
    for route in (ROUTE_RELAY, ROUTE_LEAD):
        pieces = _route_pieces(route, boundaries, spans, points, acceptance_edges)
        routes[route] = _build_segments(db, job, route, pieces, contract, top, acceptances, rate)
    rebuild_segments(db, job, [seg for route in (ROUTE_RELAY, ROUTE_LEAD) for seg in routes[route]])

    effective_route = confirmations[-1]['route'] if confirmations else None
    if effective_route not in routes:
        effective_route = None
    if effective_route is None:
        state, reason = 'pending', 'client final responsibility confirmation missing; both routes retained'
        payable, frozen = 0, coverage_minutes * rate // 60
    else:
        chosen = routes[effective_route]
        payable = sum(s['payable'] for s in chosen)
        frozen = sum(s['frozen'] for s in chosen)
        if any(s['state'] == 'blocked' for s in chosen):
            state, reason = 'disputed', 'part of the service is frozen while a responsible worker is unqualified'
        elif any(s['state'] == 'disputed' for s in chosen):
            state, reason = 'disputed', 'client dispute freezes only the identified fragment'
        elif any(s['state'] == 'pending' for s in chosen):
            # A confirmed route with no dispute still waits for acceptance,
            # unless some fragment was already paid.
            if payable:
                state, reason = 'disputed', 'part of the service awaits client acceptance and is frozen'
            else:
                state, reason = 'pending', 'client has not accepted every responsible fragment'
        else:
            state, reason = 'settled', 'relayed service, fragments and client responsibility route verified'

    result = update(db, 'settlement_decision', job, state=state, reason=reason,
                    minutes=coverage_minutes, payable=payable, frozen=frozen, mode='relay',
                    route=effective_route,
                    coverage_start=coverage_start.isoformat(), coverage_end=coverage_end.isoformat(),
                    gap_minutes=gap_minutes)
    result['routes'] = routes
    result['points'] = points
    result['spans'] = spans
    result['confirmations'] = [dict(c) for c in confirmations]
    return result


def _adjudicate_single(db, job, contract, top, spans, span_errors, acceptances, rate):
    """Single-worker segmented path; mirrors the legacy settlement arithmetic."""
    if span_errors or len(spans) != 1:
        if not span_errors:
            state, reason = 'pending', 'independent arrival and departure receipts missing'
        else:
            level = 'blocked' if any(level_ == 'blocked' for level_, _ in span_errors) else 'disputed'
            state = level
            reason = '; '.join(message for _, message in span_errors)
        rebuild_segments(db, job, [])
        return update(db, 'settlement_decision', job, state=state, reason=reason,
                      minutes=0, payable=0, frozen=0, mode='single', route=ROUTE_SINGLE,
                      coverage_start=None, coverage_end=None, gap_minutes=0)
    worker = next(iter(spans))
    span = spans[worker]
    boundaries = sorted({span['start'], span['end']}
                        | _acceptance_boundaries(acceptances, contract, top))
    pieces = []
    for a, b in zip(boundaries, boundaries[1:]):
        billable = max(0, minutes(a, b) - _overlap_minutes(a, b, span['pauses']))
        pieces.append({'start': a.isoformat(), 'end': b.isoformat(), 'owner': worker,
                       'billable': billable, 'break': True, 'relay_at': None, 'handover_id': None})
    segments = _build_segments(db, job, ROUTE_SINGLE, pieces, contract, top, acceptances, rate)
    rebuild_segments(db, job, segments)
    payable = sum(s['payable'] for s in segments)
    frozen = sum(s['frozen'] for s in segments)
    if any(s['state'] == 'blocked' for s in segments):
        state, reason = 'blocked', next(s['reason'] for s in segments if s['state'] == 'blocked')
    elif any(s['state'] == 'disputed' for s in segments):
        state, reason = 'disputed', 'client task acceptance carries unresolved dispute'
    elif any(s['state'] == 'pending' for s in segments):
        state, reason = 'pending', 'client has not accepted contracted task'
    else:
        state, reason = 'settled', 'qualified worker, independent visit and accepted task verified'
    return update(db, 'settlement_decision', job, state=state, reason=reason,
                  minutes=span['site_minutes'], payable=payable, frozen=frozen, mode='single',
                  route=ROUTE_SINGLE, coverage_start=span['start'].isoformat(),
                  coverage_end=span['end'].isoformat(), gap_minutes=0)
