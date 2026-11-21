"""Offline collection of synthetic public service-review HTML pages."""

from html.parser import HTMLParser
from .source import record


class ReviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            values = dict(attrs)
            if values.get('name') in ('review-key', 'job', 'rating', 'body'):
                self.values[values['name']] = values.get('content', '')


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('pages', []):
            parser = ReviewParser()
            parser.feed(row['html'])
            if parser.values.get('review-key') != row['review_key']:
                raise ValueError('review key mismatch')
            if record(db, row['event_id'], 'review_page', row):
                db.execute('INSERT INTO review_page VALUES(?,?,?,?,?)',
                           (row['event_id'], row['platform'], row['review_key'], row['fetched_at'], row['html']))
            touched.add((row['platform'], row['review_key']))
        for platform, key in sorted(touched):
            page = db.execute('SELECT * FROM review_page WHERE platform=? AND review_key=? ORDER BY fetched_at DESC,event_id LIMIT 1',
                              (platform, key)).fetchone()
            parser = ReviewParser()
            parser.feed(page['html'])
            values = parser.values
            db.execute('INSERT INTO review_entry VALUES(?,?,?,?,?,?) ON CONFLICT(platform,review_key) DO UPDATE SET job=excluded.job,rating=excluded.rating,body=excluded.body,source_event=excluded.source_event',
                       (platform, key, values['job'], int(values['rating']), values.get('body', ''), page['event_id']))
            from .quality import decide
            decide(db, values['job'])
    return [dict(db.execute('SELECT * FROM review_entry WHERE platform=? AND review_key=?', pair).fetchone()) for pair in sorted(touched)]
