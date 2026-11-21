"""Offline collection of synthetic public home-service offers."""

from html.parser import HTMLParser
from .source import record


class OfferParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            attrs = dict(attrs)
            if attrs.get('name') in ('vendor', 'service', 'price', 'unit', 'valid-to'):
                self.values[attrs['name']] = attrs.get('content', '')


def import_data(db, data):
    services = set()
    with db:
        for row in data.get('pages', []):
            parser = OfferParser()
            parser.feed(row['html'])
            v = parser.values
            if v.get('vendor') != row['vendor'] or v.get('service') != row['service']:
                raise ValueError('offer envelope mismatch')
            if record(db, row['event_id'], 'offer_page', row):
                db.execute('INSERT INTO offer_page VALUES(?,?,?,?,?)',
                           (row['event_id'], row['vendor'], row['service'], row['fetched_at'], row['html']))
                db.execute('INSERT INTO service_offer VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['vendor'], row['service'], int(v['price']), v['unit'], v['valid-to']))
            services.add(row['service'])
        from .pricing import decide
        return [decide(db, service) for service in sorted(services)]
