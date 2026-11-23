"""Offline collection of synthetic public service-review HTML pages.

Pages are merged into shared review claims only through platform-signed
repost mappings; identical text or image digests never merge on their own.
Edit pages are ordered by publish version, never by fetch or arrival order,
and author retractions withdraw only the page versions they explicitly name.
"""

import json
from html.parser import HTMLParser
from .source import record, offline_sign

POLICIES = ('conservative', 'signed-first')


class ReviewParser(HTMLParser):
    NAMES = ('review-key', 'job', 'rating', 'body', 'published', 'author', 'image')

    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            values = dict(attrs)
            if values.get('name') in self.NAMES:
                self.values[values['name']] = values.get('content', '')


def claim_id(platform, review_key, job):
    return f'{platform}:{review_key}:{job}'


def parse_page(row):
    parser = ReviewParser()
    parser.feed(row['html'])
    values = parser.values
    if values.get('review-key') != row['review_key']:
        raise ValueError('review key mismatch')
    if not values.get('job') or not values.get('rating'):
        raise ValueError('review page lacks job or rating')
    return {
        'job': values['job'],
        'rating': int(values['rating']),
        'body': values.get('body', ''),
        'published': values.get('published') or row['fetched_at'],
        'author': values.get('author') or None,
        'image': values.get('image') or None,
    }


def check_signature(row, kind, fields):
    payload = {'type': kind}
    payload.update({field: row[field] for field in fields})
    if row.get('signature') != offline_sign(payload):
        raise ValueError(f'{kind} signature invalid')


def policy(db):
    row = db.execute('SELECT policy FROM review_policy WHERE id=1').fetchone()
    return row['policy'] if row else 'conservative'


def record_case(db, case_id, job, subject, state, resolution, detail):
    old = db.execute('SELECT * FROM identity_case WHERE case_id=?', (case_id,)).fetchone()
    if old and (old['state'], old['resolution'], old['detail']) == (state, resolution, detail):
        return
    version = old['version'] + 1 if old else 1
    db.execute('INSERT INTO identity_case VALUES(?,?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET '
               'job=excluded.job,subject=excluded.subject,state=excluded.state,resolution=excluded.resolution,'
               'detail=excluded.detail,version=excluded.version',
               (case_id, job, subject, state, resolution, detail, version))
    db.execute('INSERT INTO decision_history(subsystem,job,version,state,reason) VALUES(?,?,?,?,?)',
               ('identity_case', job, version, state, f"{subject} {resolution or 'identity verification open'}"))


def refresh(db):
    """Recompute entries, attributions, identity cases and quality cases from evidence."""
    retracted = set()
    page_declared = {}
    for declaration in db.execute('SELECT * FROM author_declaration ORDER BY event_id').fetchall():
        targets = json.loads(declaration['pages'])
        if declaration['kind'] == 'retract':
            retracted.update(targets)
        elif declaration['kind'] == 'authorship':
            for page_id in targets:
                page_declared[page_id] = (declaration['author'], declaration['event_id'])
    groups = {}
    for page in db.execute('SELECT * FROM review_page ORDER BY published,event_id').fetchall():
        groups.setdefault((page['platform'], page['review_key'], page['job']), []).append(page)
    entries = {}
    for key, versions in groups.items():
        platform, review_key, job = key
        active = [v for v in versions if v['event_id'] not in retracted]
        effective = active[-1] if active else None
        declared = declaration_event = None
        for version in versions:
            if version['event_id'] in page_declared:
                declared, declaration_event = page_declared[version['event_id']]
        entries[key] = {
            'platform': platform, 'review_key': review_key, 'job': job,
            'rating': effective['rating'] if effective else None,
            'body': effective['body'] if effective else '',
            'source_event': effective['event_id'] if effective else None,
            'author': declared or (effective['author'] if effective else None),
            'declared_author': declared,
            'declaration_event': declaration_event,
            'published': effective['published'] if effective else None,
            'retracted': 0 if effective else 1,
            'merged_into': None, 'merge_state': None, 'pending': 0,
        }
    id_to_key = {claim_id(*key): key for key in entries}

    def root_of(key):
        while entries[key]['merged_into'] is not None:
            key = id_to_key[entries[key]['merged_into']]
        return key

    def merge(target_key, root_key, state):
        root_id = claim_id(*root_key)
        old_id = claim_id(*target_key)
        entries[target_key]['merged_into'] = root_id
        entries[target_key]['merge_state'] = state
        for other in entries.values():
            if other['merged_into'] == old_id:
                other['merged_into'] = root_id

    proofs = {}
    for proof in db.execute('SELECT * FROM order_proof ORDER BY event_id').fetchall():
        proofs.setdefault(proof['job'], []).append(proof)
    mode = policy(db)
    cases = {}
    for mapping in db.execute('SELECT * FROM repost_map ORDER BY event_id').fetchall():
        source_key = (mapping['source_platform'], mapping['source_key'], mapping['job'])
        target_key = (mapping['target_platform'], mapping['target_key'], mapping['job'])
        if source_key not in entries or target_key not in entries:
            continue
        if entries[target_key]['merged_into'] is not None:
            continue
        root_key = root_of(source_key)
        if root_key == target_key or entries[root_key]['retracted']:
            continue
        target = entries[target_key]
        root_author = entries[root_key]['author']
        contesting = [p['event_id'] for p in proofs.get(mapping['job'], [])
                      if root_author is None or p['client'] != root_author]
        declared = target['declared_author']
        case_id = f"{mapping['job']}:{target['platform']}:{target['review_key']}"
        subject = f"{target['platform']}:{target['review_key']}"
        detail = json.dumps({'repost': mapping['event_id'], 'proofs': contesting,
                             'declaration': target['declaration_event']},
                            ensure_ascii=False, sort_keys=True)
        if declared is not None and root_author is not None and declared != root_author:
            cases[case_id] = (mapping['job'], subject, 'resolved', 'independent', detail)
        elif declared is not None and declared == root_author:
            merge(target_key, root_key, 'confirmed')
            if contesting:
                cases[case_id] = (mapping['job'], subject, 'resolved', 'mirror', detail)
        elif contesting:
            if mode == 'signed-first':
                merge(target_key, root_key, 'tentative')
            else:
                target['pending'] = 1
            cases[case_id] = (mapping['job'], subject, 'open', None, detail)
        else:
            merge(target_key, root_key, 'confirmed')

    db.execute('DELETE FROM review_entry')
    for key in sorted(entries):
        e = entries[key]
        db.execute('INSERT INTO review_entry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (e['platform'], e['review_key'], e['job'], e['rating'], e['body'], e['source_event'],
                    e['author'], e['published'], e['retracted'], e['merged_into'], e['merge_state'], e['pending']))
    db.execute('DELETE FROM page_attribution')
    for key in sorted(groups):
        root_id = claim_id(*root_of(key))
        effective_event = entries[key]['source_event']
        for version in groups[key]:
            if version['event_id'] in retracted:
                role = 'retracted'
            elif version['event_id'] == effective_event:
                role = 'effective'
            else:
                role = 'superseded'
            db.execute('INSERT INTO page_attribution VALUES(?,?,?,?,?,?)',
                       (version['event_id'], version['platform'], version['review_key'],
                        version['job'], root_id, role))
    for case_id in sorted(cases):
        record_case(db, case_id, *cases[case_id])
    from .quality import decide
    for job in sorted({key[2] for key in entries}):
        decide(db, job)


def import_data(db, data):
    with db:
        if data.get('policy') is not None:
            if data['policy'] not in POLICIES:
                raise ValueError('unknown review policy')
            db.execute('INSERT INTO review_policy(id,policy) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET policy=excluded.policy',
                       (data['policy'],))
        for row in data.get('pages', []):
            parsed = parse_page(row)
            if record(db, row['event_id'], 'review_page', row):
                db.execute('INSERT INTO review_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['platform'], row['review_key'], parsed['job'], parsed['published'],
                            parsed['author'], parsed['rating'], parsed['body'], parsed['image'],
                            row['fetched_at'], row['html']))
        for row in data.get('reposts', []):
            check_signature(row, 'repost', ('source', 'target', 'job', 'signed_by'))
            if record(db, row['event_id'], 'repost_map', row):
                db.execute('INSERT INTO repost_map VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'], row['job'], row['source']['platform'], row['source']['review_key'],
                            row['target']['platform'], row['target']['review_key'], row['signed_by'], row['signature']))
        for row in data.get('declarations', []):
            check_signature(row, 'declaration', ('kind', 'author', 'platform', 'review_key', 'pages'))
            if row['kind'] not in ('retract', 'authorship'):
                raise ValueError('unknown declaration kind')
            if record(db, row['event_id'], 'author_declaration', row):
                db.execute('INSERT INTO author_declaration VALUES(?,?,?,?,?,?,?)',
                           (row['event_id'], row['kind'], row['author'], row['platform'], row['review_key'],
                            json.dumps(row['pages'], ensure_ascii=False), row['signature']))
        for row in data.get('proofs', []):
            if record(db, row['event_id'], 'order_proof', row):
                db.execute('INSERT INTO order_proof VALUES(?,?,?,?,?)',
                           (row['event_id'], row['job'], row['client'], row['order'], row['issued_by']))
        refresh(db)
    return [dict(row) for row in db.execute('SELECT * FROM review_entry ORDER BY platform,review_key,job').fetchall()]
