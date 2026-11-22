"""Material batch intake, checkout, on-site consumption and recall tracing.

Independent material preparation and usage decision per job. Batch identity is
(batch code, supplier, source document); same-named batches from different
suppliers or documents are never conflated. Reserved (checkout split) and
actually consumed (on-site usage) quantities are kept in separate events, so
stock reallocation never rewrites recorded consumption.
"""
from .source import record
from .store import update
from .contract import effective

REVIEW_STATES = ('recall_risk', 'recall_hold', 'review')


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def ledger(db):
    """Replay batch, checkout, return, recall and usage events in time order.

    Returns stock levels and per-(checkout, job) allocation facts. A checkout
    is valid only when signed, its batch known and not yet recalled, and stock
    covers it at its own timestamp. Usage and returns are valid only against
    the remaining split allocation of a valid checkout for that same job, so
    unopened stock carried in the same box is never inferred as used.
    """
    batches = {r['event_id']: dict(r) for r in db.execute('SELECT * FROM material_batch')}
    checkouts = {r['event_id']: dict(r) for r in db.execute('SELECT * FROM material_checkout')}
    splits = {}
    for r in db.execute('SELECT * FROM material_checkout_split'):
        splits.setdefault(r['checkout'], {})[r['job']] = r['quantity']
    events = []
    for c in checkouts.values():
        events.append((c['at'], 0, c['event_id'], 'checkout', c))
    for r in _rows(db, 'SELECT * FROM material_return'):
        events.append((r['at'], 1, r['event_id'], 'return', r))
    for r in _rows(db, 'SELECT * FROM material_recall'):
        events.append((r['at'], 2, r['event_id'], 'recall', r))
    for r in _rows(db, 'SELECT * FROM material_usage'):
        events.append((r['at'], 3, r['event_id'], 'usage', r))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    available = {b: row['quantity'] for b, row in batches.items()}
    quarantined = {b: 0 for b in batches}
    recalled = set()
    valid_checkouts = set()
    checkout_invalid = {}
    alloc = {}

    def slot(cid, job):
        return alloc.setdefault((cid, job), {'split': 0, 'allocated': 0, 'consumed': 0,
                                             'returned_unopened': 0, 'returned_opened': 0, 'invalid': 0})

    for _at, _rank, eid, kind, ev in events:
        if kind == 'checkout':
            b = ev['batch_event']
            for job, qty in splits.get(eid, {}).items():
                slot(eid, job)['split'] += qty
            if not ev['received_by']:
                checkout_invalid[eid] = 'unsigned'
            elif b not in batches:
                checkout_invalid[eid] = 'unknown_batch'
            elif b in recalled:
                checkout_invalid[eid] = 'recalled'
            elif available[b] < ev['quantity']:
                checkout_invalid[eid] = 'stock'
            else:
                valid_checkouts.add(eid)
                available[b] -= ev['quantity']
                for job, qty in splits.get(eid, {}).items():
                    slot(eid, job)['allocated'] += qty
        elif kind == 'recall':
            recalled.add(ev['batch_event'])
        else:
            cid = ev['checkout']
            c = checkouts.get(cid)
            s = slot(cid, ev['job'])
            remaining = s['allocated'] - s['consumed'] - s['returned_unopened'] - s['returned_opened']
            if cid not in valid_checkouts or ev['quantity'] > remaining:
                s['invalid'] += ev['quantity']
            elif kind == 'usage':
                s['consumed'] += ev['quantity']
            elif ev['opened']:
                s['returned_opened'] += ev['quantity']
                quarantined[c['batch_event']] += ev['quantity']
            else:
                s['returned_unopened'] += ev['quantity']
                b = c['batch_event']
                if b in recalled:
                    quarantined[b] += ev['quantity']
                else:
                    available[b] += ev['quantity']
    return {'batches': batches, 'checkouts': checkouts, 'available': available,
            'quarantined': quarantined, 'recalled': recalled, 'valid_checkouts': valid_checkouts,
            'checkout_invalid': checkout_invalid, 'alloc': alloc}


def _covers(batch, window):
    if not window:
        return True
    return batch['valid_from'] <= window[0] and batch['valid_to'] >= window[1]


def _job_stock(led, job):
    consumed, held, invalid = {}, {}, 0
    for (cid, j), s in led['alloc'].items():
        if j != job:
            continue
        invalid += s['invalid']
        c = led['checkouts'].get(cid)
        if not c or cid not in led['valid_checkouts']:
            continue
        b = c['batch_event']
        if s['consumed']:
            consumed[b] = consumed.get(b, 0) + s['consumed']
        left = s['allocated'] - s['consumed'] - s['returned_unopened'] - s['returned_opened']
        if left > 0:
            held[b] = held.get(b, 0) + left
    return consumed, held, invalid


def _plan_recalled(plans, recalled, batches):
    ids = {(batches[b]['batch'], batches[b]['supplier'], batches[b]['source_doc'])
           for b in recalled if b in batches}
    return any((p.get('batch'), p.get('supplier'), p.get('source_doc')) in ids
               for p in plans if p.get('batch'))


def _stock_for(led, materials, window):
    return sum(q for b, q in led['available'].items()
               if b in led['batches'] and b not in led['recalled']
               and led['batches'][b]['material'] in materials and _covers(led['batches'][b], window))


def decide(db, job):
    led = ledger(db)
    plans = _rows(db, 'SELECT * FROM material_plan WHERE job=? ORDER BY event_id', (job,))
    routes = _rows(db, 'SELECT * FROM material_route WHERE job=? ORDER BY at,event_id', (job,))
    authzs = _rows(db, 'SELECT * FROM material_authz WHERE job=?', (job,))
    mine = {k: v for k, v in led['alloc'].items() if k[1] == job}
    if not plans and not mine and not routes and not authzs:
        return None
    contract, top, condition = effective(db, job)
    window = (contract['start'], contract['end']) if contract and condition == 'valid' else None
    batches, recalled = led['batches'], led['recalled']
    consumed, held, invalid = _job_stock(led, job)
    required = sum(p['quantity'] for p in plans) or (1 if mine else 0)
    route = routes[-1] if routes else None
    client = top['client'] if top else None
    granted = {a['kind'] for a in authzs if client is None or a['client'] == client}
    used_recalled = sorted(b for b in consumed if b in recalled)
    split_cids = [cid for (cid, _), s in mine.items() if s['split'] > 0]
    if used_recalled:
        names = ','.join(batches[b]['batch'] for b in used_recalled if b in batches)
        state, reason = 'recall_risk', f'recalled batch consumed on site: {names}'
    elif invalid:
        state, reason = 'review', 'consumption or return does not match a valid signed checkout allocation'
    elif route and route['choice'] == 'reschedule' and 'reschedule' in granted:
        state, reason = 'deferred', 'client authorized reschedule; checkout released and unsealing forbidden until new window'
    elif route and route['choice'] == 'substitute' and 'substitute' in granted and (
            _plan_recalled(plans, recalled, batches) or any(b in recalled for b in held)):
        materials = {p['material'] for p in plans} or {batches[b]['material'] for b in (*held, *consumed) if b in batches}
        equiv = sum(q for b, q in consumed.items() if b not in recalled and batches[b]['material'] in materials)
        equiv += sum(q for b, q in held.items()
                     if b not in recalled and batches[b]['material'] in materials and _covers(batches[b], window))
        if equiv >= required:
            state, reason = 'ready', 'equivalent batch substituted under client authorization'
        elif equiv + _stock_for(led, materials, window) >= required:
            state, reason = 'pending', 'substitution authorized; equivalent checkout pending'
        else:
            state, reason = 'shortage', 'no equivalent batch available for authorized substitution'
    elif any(b in recalled for b in held):
        state, reason = 'recall_hold', 'allocated batch recalled by supplier; unsealing forbidden pending review'
    elif split_cids and not any(cid in led['valid_checkouts'] for cid in split_cids) and not consumed:
        reasons = {led['checkout_invalid'].get(cid) for cid in split_cids}
        if reasons == {'unsigned'}:
            state, reason = 'unsigned', 'checkout handover missing recipient signature'
        elif 'recalled' in reasons:
            state, reason = 'recall_hold', 'allocated batch recalled by supplier; unsealing forbidden pending review'
        else:
            state, reason = 'shortage', 'available batch quantity below planned requirement'
    elif required == 0 and not consumed and not any(s['allocated'] for s in mine.values()):
        state, reason = 'pending', 'material preparation not recorded'
    else:
        fresh = {b: q for b, q in held.items() if b not in recalled and _covers(batches[b], window)}
        expired = {b: q for b, q in held.items() if b not in recalled and not _covers(batches[b], window)}
        fulfilled = sum(fresh.values()) + sum(q for b, q in consumed.items() if b not in recalled)
        if fulfilled >= required:
            state, reason = 'ready', 'allocated batch valid and sufficient'
        elif expired and not fresh:
            state, reason = 'expired', 'batch validity does not cover service window'
        else:
            materials = {p['material'] for p in plans}
            if fulfilled + _stock_for(led, materials, window) >= required:
                state, reason = 'pending', 'material preparation or checkout still incomplete'
            else:
                state, reason = 'shortage', 'available batch quantity below planned requirement'
    return update(db, 'material_decision', job, state=state, reason=reason)


def jobs(db):
    found = set()
    for table in ('material_plan', 'material_checkout_split', 'material_usage',
                  'material_return', 'material_route', 'material_authz'):
        found.update(r[0] for r in db.execute(f'SELECT DISTINCT job FROM {table}'))
    return found


def _pack(led, stock):
    return [{'batch': led['batches'][b]['batch'], 'material': led['batches'][b]['material'],
             'supplier': led['batches'][b]['supplier'], 'source_doc': led['batches'][b]['source_doc'],
             'quantity': q}
            for b, q in sorted(stock.items()) if q > 0 and b in led['batches']]


def job_detail(db, job):
    row = db.execute('SELECT * FROM material_decision WHERE job=?', (job,)).fetchone()
    if not row:
        return None
    led = ledger(db)
    consumed, held, _invalid = _job_stock(led, job)
    item = dict(row)
    item['consumed'] = _pack(led, consumed)
    item['held'] = _pack(led, held)
    return item


def overview(db):
    led = ledger(db)
    batches = []
    for b, row in sorted(led['batches'].items()):
        cids = {c['event_id'] for c in led['checkouts'].values() if c['batch_event'] == b}
        checked_out = sum(c['quantity'] for c in led['checkouts'].values()
                          if c['batch_event'] == b and c['event_id'] in led['valid_checkouts'])
        consumed = sum(s['consumed'] for (cid, _), s in led['alloc'].items() if cid in cids)
        returned = sum(s['returned_unopened'] for (cid, _), s in led['alloc'].items() if cid in cids)
        batches.append({'event_id': b, 'batch': row['batch'], 'material': row['material'],
                        'task': row['task'], 'supplier': row['supplier'], 'source_doc': row['source_doc'],
                        'quantity': row['quantity'], 'checked_out': checked_out, 'consumed': consumed,
                        'returned_unopened': returned, 'quarantined': led['quarantined'][b],
                        'available': led['available'][b], 'recalled': b in led['recalled']})
    recalls = []
    for r in _rows(db, 'SELECT * FROM material_recall ORDER BY event_id'):
        b = r['batch_event']
        row = led['batches'].get(b)
        cids = {c['event_id'] for c in led['checkouts'].values() if c['batch_event'] == b}
        consumed_by = sorted({j for (cid, j), s in led['alloc'].items()
                              if cid in cids and s['consumed'] > 0})
        held_by = sorted({j for (cid, j), s in led['alloc'].items()
                          if cid in cids and s['allocated'] - s['consumed']
                          - s['returned_unopened'] - s['returned_opened'] > 0})
        recalls.append({'event_id': r['event_id'], 'batch_event': b,
                        'batch': row['batch'] if row else None, 'supplier': r['supplier'],
                        'at': r['at'], 'reason': r['reason'],
                        'consumed_by': consumed_by, 'held_by': held_by})
    pending = sorted(r['job'] for r in db.execute(
        f"SELECT job FROM material_decision WHERE state IN ({','.join('?' for _ in REVIEW_STATES)})",
        REVIEW_STATES))
    return {'batches': batches, 'recalls': recalls, 'pending_review': pending}


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('batches', []):
            if record(db, row['event_id'], 'batch', row):
                dup = db.execute('SELECT event_id FROM material_batch WHERE batch=? AND supplier=? AND source_doc=?',
                                 (row['batch'], row['supplier'], row['source_doc'])).fetchone()
                if dup:
                    raise ValueError(f'batch identity conflict: {row["event_id"]} duplicates {dup["event_id"]}')
                db.execute('INSERT INTO material_batch VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['batch'], row['material'], row['task'], row['supplier'],
                            row['source_doc'], row['quantity'], row['valid_from'], row['valid_to']))
        for row in data.get('plans', []):
            if record(db, row['event_id'], 'plan', row):
                db.execute('INSERT INTO material_plan VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['job'], row['material'], row['task'], row['quantity'],
                            row.get('batch'), row.get('supplier'), row.get('source_doc')))
            touched.add(row['job'])
        for row in data.get('checkouts', []):
            if record(db, row['event_id'], 'checkout', row):
                db.execute('INSERT INTO material_checkout VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['batch_event'], row['worker'], row['quantity'], row['at'],
                            row['issued_by'], row.get('received_by')))
                for split in row.get('splits', []):
                    db.execute('INSERT INTO material_checkout_split VALUES(?,?,?)',
                               (row['event_id'], split['job'], split['quantity']))
                    touched.add(split['job'])
        for row in data.get('usages', []):
            if record(db, row['event_id'], 'usage', row):
                db.execute('INSERT INTO material_usage VALUES(?,?,?,?,?)',
                           (row['event_id'], row['job'], row['checkout'], row['quantity'], row['at']))
            touched.add(row['job'])
        for row in data.get('returns', []):
            if record(db, row['event_id'], 'return', row):
                db.execute('INSERT INTO material_return VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['job'], row['checkout'], row['quantity'],
                            int(row['opened']), row['at']))
            touched.add(row['job'])
        for row in data.get('recalls', []):
            if record(db, row['event_id'], 'recall', row):
                db.execute('INSERT INTO material_recall VALUES(?,?,?,?,?)',
                           (row['event_id'], row['batch_event'], row['supplier'], row['at'], row['reason']))
        for row in data.get('authorizations', []):
            if record(db, row['event_id'], 'authorization', row):
                db.execute('INSERT INTO material_authz VALUES(?,?,?,?)',
                           (row['event_id'], row['job'], row['kind'], row['client']))
            touched.add(row['job'])
        for row in data.get('routes', []):
            if record(db, row['event_id'], 'route', row):
                db.execute('INSERT INTO material_route VALUES(?,?,?,?)',
                           (row['event_id'], row['job'], row['choice'], row['at']))
            touched.add(row['job'])
        touched.update(jobs(db))
        return [r for r in (decide(db, job) for job in sorted(touched)) if r]
