"""Evidence-verified market quotes from collected public offers.

A market_quote row is only the current summary for one (service, region).
Every recomputed state is appended to quote_version; every quote handed to
a customer is an immutable quote_delivery snapshot keyed by quote version
and recipient. States: quoted (legacy page), verified (已核), pending (待证),
disputed (争议), expired (失效).
"""

import json
from .source import record

DELIVERABLE = ('verified', 'quoted')


def summarize(db, service, region, state, price, unit, amount, source_event, proofs, reason):
    proofs = json.dumps(proofs, ensure_ascii=False)
    old = db.execute('SELECT * FROM market_quote WHERE service=? AND region=?', (service, region)).fetchone()
    if old and (old['state'], old['price'], old['unit'], old['amount'], old['source_event'], old['proofs'], old['reason']) == \
            (state, price, unit, amount, source_event, proofs, reason):
        return dict(old)
    version = old['version'] + 1 if old else 1
    db.execute('INSERT INTO market_quote VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(service,region) DO UPDATE SET '
               'state=excluded.state,price=excluded.price,unit=excluded.unit,amount=excluded.amount,'
               'source_event=excluded.source_event,proofs=excluded.proofs,reason=excluded.reason,version=excluded.version',
               (service, region, state, price, unit, amount, source_event, proofs, reason, version))
    db.execute('INSERT INTO quote_version(service,region,version,state,price,unit,amount,source_event,proofs,reason) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)',
               (service, region, version, state, price, unit, amount, source_event, proofs, reason))
    return dict(db.execute('SELECT * FROM market_quote WHERE service=? AND region=?', (service, region)).fetchone())


def receipt_proof(db, page):
    """A signed settlement page plus a matching order receipt for the same
    customer, same vendor/service/region and inside the page's valid window,
    whose numbers reproduce the page terms. Another customer's or another
    region's deal never proves this page."""
    rows = db.execute('SELECT * FROM settlement_receipt WHERE vendor=? AND service=? AND region=? AND signed_by=? '
                      'ORDER BY event_id', (page['vendor'], page['service'], page['region'], page['vendor'])).fetchall()

    def applies(r):
        return (page['valid_from'] <= r['settled_at'] <= page['valid_to']
                and r['unit_price'] == page['price'] and r['callout_fee'] == page['callout_fee']
                and r['hours'] >= page['min_hours']
                and r['total'] == r['unit_price'] * r['hours'] + r['callout_fee'])

    settlements = {r['customer']: r for r in rows if r['kind'] == 'settlement_page' and applies(r)}
    orders = {r['customer']: r for r in rows if r['kind'] == 'order_receipt' and applies(r)}
    common = sorted(set(settlements) & set(orders))
    if not common:
        return None, []
    customer = common[0]
    amount = page['price'] * page['min_hours'] + page['callout_fee']
    return amount, [settlements[customer]['event_id'], orders[customer]['event_id']]


def applicable_errata(db, page):
    rows = db.execute('SELECT * FROM offer_errata WHERE service=? AND region=? ORDER BY issued_at,event_id',
                      (page['service'], page['region'])).fetchall()
    found = []
    for e in rows:
        if not (e['supersedes'] == page['event_id'] and e['vendor'] == page['vendor'] and e['signed_by'] == page['vendor']):
            continue
        lo, hi = e['valid_from'] or page['valid_from'], e['valid_to'] or '9999'
        if lo <= page['valid_to'] and hi >= page['valid_from']:
            found.append(e)
    return found


def decide_terms(db, service, region, pages):
    retractions = db.execute('SELECT * FROM offer_retraction WHERE service=? AND region=? ORDER BY event_id',
                             (service, region)).fetchall()
    dead = {r['retracts'] for r in retractions} | {p['supersedes'] for p in pages if p['supersedes']}
    active = [p for p in pages if p['event_id'] not in dead]
    if not active:
        proofs = [{'kind': 'offer_page', 'event_id': p['event_id']} for p in pages]
        proofs += [{'kind': 'offer_retraction', 'event_id': r['event_id']} for r in retractions]
        if pages:
            return summarize(db, service, region, 'expired', 0, '', None, pages[-1]['event_id'], proofs,
                             'offer revoked by a later page; delivered quote proofs stay unchanged')
        return summarize(db, service, region, 'pending', 0, '', None, None, proofs, 'offer page missing')
    page = active[-1]
    price, unit = page['price'], page['unit']
    proofs = [{'kind': 'offer_page', 'event_id': page['event_id']}]
    if page['region_lock'] and region not in page['region_lock'].split(','):
        return summarize(db, service, region, 'pending', price, unit, None, page['event_id'], proofs,
                         'page regional restriction excludes this region')
    candidates = {}
    errata = applicable_errata(db, page)
    if {bool(e['waive_callout_fee']) for e in errata} == {False, True}:
        proofs += [{'kind': 'offer_errata', 'event_id': e['event_id']} for e in errata]
        return summarize(db, service, region, 'disputed', price, unit, None, page['event_id'], proofs,
                         'signed errata contradict each other on the committed fee')
    if errata:
        fee = 0 if errata[0]['waive_callout_fee'] else page['callout_fee']
        candidates['errata'] = price * page['min_hours'] + fee
        proofs += [{'kind': 'offer_errata', 'event_id': e['event_id']} for e in errata]
    amount, receipt_ids = receipt_proof(db, page)
    if amount is not None:
        candidates['receipts'] = amount
        proofs += [{'kind': 'settlement_receipt', 'event_id': e} for e in receipt_ids]
    distinct = set(candidates.values())
    if len(distinct) > 1:
        return summarize(db, service, region, 'disputed', price, unit, None, page['event_id'], proofs,
                         'signed proofs conflict on the committed amount')
    if len(distinct) == 1:
        return summarize(db, service, region, 'verified', price, unit, distinct.pop(), page['event_id'], proofs,
                         'commitment proven by signed source evidence')
    if page['callout_uncertain']:
        return summarize(db, service, region, 'pending', price, unit, None, page['event_id'], proofs,
                         'body and footnote disagree on call-out fee; supplementary proof required')
    return summarize(db, service, region, 'verified', price, unit, price * page['min_hours'] + page['callout_fee'],
                     page['event_id'], proofs, 'page body and footnotes consistent')


def decide(db, service, region=''):
    pages = db.execute('SELECT * FROM offer_terms WHERE service=? AND region=? ORDER BY valid_from,event_id',
                       (service, region)).fetchall()
    if pages or region:
        return decide_terms(db, service, region, pages)
    rows = db.execute('SELECT * FROM service_offer WHERE service=? ORDER BY price,event_id', (service,)).fetchall()
    if rows:
        best = rows[0]
        return summarize(db, service, '', 'quoted', best['price'], best['unit'], best['price'], best['event_id'],
                         [{'kind': 'offer_page', 'event_id': best['event_id']}], 'lowest collected public offer')
    return summarize(db, service, '', 'pending', 0, '', None, None, [], 'no collected public offer')


def deliver(db, row):
    """Record an immutable delivery of the current quote to a recipient.
    Only a verified (or legacy quoted) summary may be handed out; the
    snapshot keeps its original form even if the quote is later disputed
    or revoked."""
    service, region = row['service'], row.get('region', '')
    payload = {'event_id': row['event_id'], 'service': service, 'region': region,
               'recipient': row['recipient'], 'at': row['at']}
    if not record(db, row['event_id'], 'quote_delivery', payload):
        return None
    quote = db.execute('SELECT * FROM market_quote WHERE service=? AND region=?', (service, region)).fetchone()
    if not quote or quote['state'] not in DELIVERABLE:
        raise ValueError(f"quote not deliverable for {service}/{region}: {quote['state'] if quote else 'missing'}")
    amount = quote['amount'] if quote['amount'] is not None else quote['price']
    terms = {'price': quote['price'], 'unit': quote['unit'], 'amount': amount}
    page = db.execute('SELECT * FROM offer_terms WHERE event_id=?', (quote['source_event'],)).fetchone()
    if page:
        terms.update(min_hours=page['min_hours'], callout_fee=page['callout_fee'], campaign=page['campaign'],
                     valid_from=page['valid_from'], valid_to=page['valid_to'])
    db.execute('INSERT INTO quote_delivery VALUES(?,?,?,?,?,?,?,?,?,?)',
               (row['event_id'], service, region, row['recipient'], quote['version'], quote['state'], amount,
                json.dumps(terms, ensure_ascii=False), quote['proofs'], row['at']))
    return dict(db.execute('SELECT * FROM quote_delivery WHERE event_id=?', (row['event_id'],)).fetchone())


def _decode(row, *keys):
    for key in keys:
        row[key] = json.loads(row[key])
    return row


def quote_detail(db, service, region=''):
    quote = db.execute('SELECT * FROM market_quote WHERE service=? AND region=?', (service, region)).fetchone()
    versions = db.execute('SELECT * FROM quote_version WHERE service=? AND region=? ORDER BY version',
                          (service, region)).fetchall()
    deliveries = db.execute('SELECT * FROM quote_delivery WHERE service=? AND region=? ORDER BY event_id',
                            (service, region)).fetchall()
    return {'quote': _decode(dict(quote), 'proofs') if quote else None,
            'versions': [_decode(dict(r), 'proofs') for r in versions],
            'deliveries': [_decode(dict(r), 'terms', 'proofs') for r in deliveries]}
