"""Offline collection of synthetic public home-service offers.

Pages are collected with their body and footnote terms (minimum hours,
call-out fee, regional restriction, discount exclusion). fetched_at only
records when the page was seen; the committed window, region and version
come from the page itself or from a signed errata. Independent settlement
receipts and vendor-sealed errata are collected as separate evidence.
"""

import json
from html.parser import HTMLParser
from .source import record

META_NAMES = ('vendor', 'service', 'price', 'unit', 'valid-to',
              'region', 'valid-from', 'campaign', 'supersedes', 'retracts')
TERM_ATTRS = (('min_hours', 'data-min-hours'), ('callout_fee', 'data-callout-fee'),
              ('region_lock', 'data-regions'), ('promo_exclusive', 'data-promo-exclusive'),
              ('maybe', 'data-maybe'))
TRUTHY = ('1', 'true', 'yes')


class OfferParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.terms = []
        self.footnotes = []
        self._capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            if attrs.get('name') in META_NAMES:
                self.values[attrs['name']] = attrs.get('content', '')
            return
        if self._capture is not None:
            self._capture[1] += 1
            return
        term = {key: attrs[attr] for key, attr in TERM_ATTRS if attr in attrs}
        is_footnote = 'footnote' in attrs.get('class', '').split()
        if term:
            term['footnote'] = is_footnote
            self.terms.append(term)
        if is_footnote:
            self._capture = [[], 1]

    def handle_data(self, data):
        if self._capture is not None:
            self._capture[0].append(data)

    def handle_endtag(self, tag):
        if self._capture is not None:
            self._capture[1] -= 1
            if self._capture[1] == 0:
                self.footnotes.append(''.join(self._capture[0]).strip())
                self._capture = None


def new_format(row, values, parser):
    return ('region' in row or parser.terms or parser.footnotes
            or any(k in values for k in ('region', 'valid-from', 'campaign', 'supersedes')))


def aggregate(parser):
    v = parser.values
    min_hours, callout_fee, uncertain, promo = 1, 0, False, False
    locks = set()
    for term in parser.terms:
        if 'min_hours' in term:
            min_hours = max(min_hours, int(term['min_hours']))
        if 'callout_fee' in term:
            callout_fee = max(callout_fee, int(term['callout_fee']))
            if term.get('maybe', '').lower() in TRUTHY:
                uncertain = True
        if 'region_lock' in term:
            locks.update(x for x in term['region_lock'].split(',') if x)
        if term.get('promo_exclusive', '').lower() in TRUTHY:
            promo = True
    return {'price': int(v['price']), 'unit': v['unit'], 'min_hours': min_hours,
            'callout_fee': callout_fee, 'callout_uncertain': uncertain,
            'region_lock': ','.join(sorted(locks)), 'promo_exclusive': promo,
            'campaign': v.get('campaign', ''), 'valid_from': v.get('valid-from', ''),
            'valid_to': v['valid-to'], 'supersedes': v.get('supersedes', ''),
            'footnotes': parser.footnotes}


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('pages', []):
            parser = OfferParser()
            parser.feed(row['html'])
            parser.close()
            v = parser.values
            if v.get('vendor') != row['vendor'] or v.get('service') != row['service']:
                raise ValueError('offer envelope mismatch')
            region = row.get('region', '')
            if v.get('region', region) != region:
                raise ValueError('offer envelope mismatch')
            if record(db, row['event_id'], 'offer_page', row):
                db.execute('INSERT INTO offer_page VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['vendor'], row['service'], region, row['fetched_at'], row['html']))
                if v.get('retracts'):
                    db.execute('INSERT INTO offer_retraction VALUES(?,?,?,?,?,?)',
                               (row['event_id'], row['vendor'], row['service'], region, v['retracts'], row['fetched_at']))
                elif new_format(row, v, parser):
                    t = aggregate(parser)
                    db.execute('INSERT INTO offer_terms VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (row['event_id'], row['vendor'], row['service'], region, t['price'], t['unit'],
                                t['min_hours'], t['callout_fee'], int(t['callout_uncertain']), t['region_lock'],
                                int(t['promo_exclusive']), t['campaign'], t['valid_from'], t['valid_to'],
                                t['supersedes'], json.dumps(t['footnotes'], ensure_ascii=False)))
                else:
                    db.execute('INSERT INTO service_offer VALUES(?,?,?,?,?,?)',
                               (row['event_id'], row['vendor'], row['service'], int(v['price']), v['unit'], v['valid-to']))
            touched.add((row['service'], region))
        for row in data.get('receipts', []):
            if row.get('signed_by') != row.get('vendor'):
                raise ValueError('settlement receipt lacks vendor signature')
            if record(db, row['event_id'], 'settlement_receipt', row):
                db.execute('INSERT INTO settlement_receipt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['kind'], row['vendor'], row['service'], row.get('region', ''),
                            row.get('campaign', ''), row['customer'], int(row['hours']), int(row['unit_price']),
                            int(row['callout_fee']), int(row['total']), row['settled_at'], row['signed_by']))
            touched.add((row['service'], row.get('region', '')))
        for row in data.get('errata', []):
            if row.get('signed_by') != row.get('vendor'):
                raise ValueError('errata lacks vendor signature')
            if not row.get('supersedes'):
                raise ValueError('errata must name the page version it replaces')
            if record(db, row['event_id'], 'offer_errata', row):
                db.execute('INSERT INTO offer_errata VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['vendor'], row['service'], row.get('region', ''), row['supersedes'],
                            int(bool(row.get('waive_callout_fee'))), row.get('note', ''), row.get('valid_from', ''),
                            row.get('valid_to', ''), row['signed_by'], row['issued_at']))
            touched.add((row['service'], row.get('region', '')))
            page = db.execute('SELECT service,region FROM offer_terms WHERE event_id=?', (row['supersedes'],)).fetchone()
            if page:
                touched.add((page['service'], page['region']))
        from .pricing import decide, deliver
        for row in data.get('deliveries', []):
            deliver(db, row)
        return [decide(db, service, region) for service, region in sorted(touched)]
