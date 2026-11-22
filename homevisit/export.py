"""Scoped, verifiable data-access packages for customer data requests.

A package is cut per recipient from the persisted evidence and decision
history of one job.  Every field is either disclosed with a source anchor
(evidence event sha + JSON pointer, or a decision-history version) or masked
with a per-field reason.  Raw source files and evidence bodies are never
embedded: the sha anchors let an offline verifier holding the original
database confirm each disclosed field without exposing other jobs that share
the same source file.  Fields whose own value mixes another job's identifiers
cannot be split safely; they are blocked and listed in ``pending`` instead of
leaking the whole raw record.
"""

import hashlib
import json

from .contract import effective_detail

ALL_TOKENS = ('terms', 'address', 'schedule', 'acceptance', 'dispute', 'worker', 'decisions')
CERT_REASON = 'worker certificate credentials are internal'
BLOCK_REASON = 'source mixes identifiers of another job and cannot be split safely'
PENDING_REASON = 'awaiting a split source that isolates this job'

FIELD_SCOPE = {
    'contract': {'client': 'terms', 'address': 'address', 'start': 'terms', 'end': 'terms',
                 'task': 'terms', 'rate': 'terms', 'signed_by': 'terms', 'version': 'terms'},
    'receipt': {'kind': 'schedule', 'at': 'schedule', 'address': 'address',
                'worker': 'worker', 'client': 'terms', 'task': 'terms'},
    'acceptance': {'task': 'acceptance', 'accepted': 'acceptance', 'client': 'acceptance',
                   'dispute': 'dispute'},
    'assignment': {'worker': 'worker', 'at': 'schedule', 'client_approved': 'worker',
                   'replace_of': 'worker'},
    'decision': {'state': 'decisions', 'reason': 'decisions', 'version': 'decisions'},
    'certificate': {},
}


def canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def parse_scope(request):
    raw = request['scope'] or ''
    tokens = {token for token in raw.replace('+', ',').replace(' ', ',').split(',') if token}
    if 'own-service' in tokens:
        if request['recipient'] != request['requester']:
            return None, 'own-service scope is limited to the requester'
        tokens = (tokens - {'own-service'}) | set(ALL_TOKENS)
    unknown = sorted(tokens - set(ALL_TOKENS))
    if unknown:
        return None, f'unrecognized scope token: {unknown[0]}'
    return tokens, None


def logical_now(db):
    """Deterministic clock: latest timestamp persisted in the ledger."""
    stamps = []
    for sql in ('SELECT MAX(at) FROM visit_receipt', 'SELECT MAX(at) FROM assignment',
                'SELECT MAX(start) FROM service_contract', 'SELECT MAX(end) FROM service_contract',
                'SELECT MAX(start) FROM contract_change', 'SELECT MAX(end) FROM contract_change'):
        value = db.execute(sql).fetchone()[0]
        if value:
            stamps.append(value)
    return max(stamps) if stamps else None


def other_job_tokens(db, job):
    tokens = set()
    for sql in ('SELECT DISTINCT job AS token FROM service_contract WHERE job<>?',
                'SELECT DISTINCT client AS token FROM service_contract WHERE job<>?',
                'SELECT DISTINCT address AS token FROM service_contract WHERE job<>?',
                'SELECT DISTINCT job AS token FROM visit_receipt WHERE job<>?',
                'SELECT DISTINCT address AS token FROM visit_receipt WHERE job<>?',
                'SELECT DISTINCT client AS token FROM visit_receipt WHERE job<>? AND client IS NOT NULL',
                'SELECT DISTINCT client AS token FROM task_acceptance WHERE job<>?'):
        for row in db.execute(sql, (job,)):
            if row['token']:
                tokens.add(row['token'])
    return tokens


def mixes_other_job(value, others):
    return isinstance(value, str) and any(token in value for token in others)


def collect(db, job, tokens):
    others = other_job_tokens(db, job)
    evidence_rows = {row['event_id']: row for row in db.execute('SELECT event_id,kind,sha FROM evidence')}
    evidence = {}
    records, redactions, pending, decisions = [], [], [], []

    def anchor(source):
        event_id = source.get('event_id') if source else None
        if event_id and event_id in evidence_rows:
            row = evidence_rows[event_id]
            evidence[event_id] = {'event_id': event_id, 'kind': row['kind'], 'sha': row['sha']}

    def put(fields, name, field, value, token, source):
        if token is not None and token in tokens and not mixes_other_job(value, others):
            fields[field] = {'status': 'disclosed', 'value': value, 'source': source}
            anchor(source)
            return
        if token is None:
            status, reason = 'redacted', CERT_REASON
        elif token not in tokens:
            status, reason = 'redacted', f"scope '{token}' not granted to recipient"
        else:
            status, reason = 'blocked', BLOCK_REASON
            pending.append({'record': name, 'field': field, 'reason': PENDING_REASON, 'source': source})
            anchor(source)
        fields[field] = {'status': status, 'reason': reason}
        redactions.append({'record': name, 'field': field, 'reason': reason})

    current, top, condition, sources = effective_detail(db, job)
    if current or top:
        signed = current if current else top
        name = f'contract:{job}'
        fields = {}
        for field in ('client', 'address', 'start', 'end', 'task', 'rate', 'signed_by', 'version'):
            event_id = sources[field] if current else top['event_id']
            put(fields, name, field, signed[field], FIELD_SCOPE['contract'][field],
                {'event_id': event_id, 'pointer': f'/{field}'})
        records.append({'record': name, 'kind': 'contract', 'fields': fields})

    for row in db.execute('SELECT * FROM visit_receipt WHERE job=? ORDER BY at,event_id', (job,)):
        name = f"receipt:{row['event_id']}"
        fields = {}
        for field in ('kind', 'at', 'address', 'worker', 'client', 'task'):
            if row[field] is None:
                continue
            put(fields, name, field, row[field], FIELD_SCOPE['receipt'][field],
                {'event_id': row['event_id'], 'pointer': f'/{field}'})
        records.append({'record': name, 'kind': 'receipt', 'fields': fields})

    for row in db.execute('SELECT * FROM task_acceptance WHERE job=? ORDER BY event_id', (job,)):
        name = f"acceptance:{row['event_id']}"
        fields = {}
        values = {'task': row['task'], 'accepted': bool(row['accepted']),
                  'client': row['client'], 'dispute': row['dispute']}
        for field, value in values.items():
            if value is None:
                continue
            put(fields, name, field, value, FIELD_SCOPE['acceptance'][field],
                {'event_id': row['event_id'], 'pointer': f'/{field}'})
        records.append({'record': name, 'kind': 'acceptance', 'fields': fields})

    for row in db.execute('SELECT * FROM assignment WHERE job=? ORDER BY at,event_id', (job,)):
        name = f"assignment:{row['event_id']}"
        fields = {}
        values = {'worker': row['worker'], 'at': row['at'],
                  'client_approved': row['client_approved'], 'replace_of': row['replace_of']}
        for field, value in values.items():
            if value is None:
                continue
            put(fields, name, field, value, FIELD_SCOPE['assignment'][field],
                {'event_id': row['event_id'], 'pointer': f'/{field}'})
        records.append({'record': name, 'kind': 'assignment', 'fields': fields})

    workers = [row['worker'] for row in
               db.execute('SELECT DISTINCT worker FROM assignment WHERE job=? ORDER BY worker', (job,))]
    for worker in workers:
        for row in db.execute('SELECT * FROM worker_certificate WHERE worker=? ORDER BY event_id', (worker,)):
            name = f"certificate:{row['event_id']}"
            fields = {}
            for field in ('worker', 'task', 'valid_from', 'valid_to', 'issuer'):
                put(fields, name, field, None, None, None)
            records.append({'record': name, 'kind': 'certificate', 'fields': fields})

    for table in ('contract_decision', 'dispatch_decision', 'settlement_decision'):
        row = db.execute(f'SELECT * FROM {table} WHERE job=?', (job,)).fetchone()
        if not row:
            continue
        name = f'decision:{table}:{job}'
        fields = {}
        for field in ('state', 'reason', 'version'):
            put(fields, name, field, row[field], 'decisions',
                {'decision': table, 'version': row['version']})
        if any(cell['status'] == 'disclosed' for cell in fields.values()):
            decisions.append({'subsystem': table, 'job': job, 'version': row['version'],
                              'state': row['state'], 'reason': row['reason']})
        records.append({'record': name, 'kind': 'decision', 'fields': fields})

    return records, redactions, pending, sorted(evidence.values(), key=lambda item: item['event_id']), decisions


def build_content(db, job, tokens):
    records, redactions, pending, evidence, decisions = collect(db, job, tokens)
    return {'created_from': {'evidence': evidence, 'decisions': decisions},
            'records': records, 'redactions': redactions, 'pending': pending}


def content_stats(records):
    cells = [cell for record in records for cell in record['fields'].values()]
    return {'records': len(records),
            'disclosed': sum(1 for cell in cells if cell['status'] == 'disclosed'),
            'redacted': sum(1 for cell in cells if cell['status'] == 'redacted'),
            'blocked': sum(1 for cell in cells if cell['status'] == 'blocked')}


def deliver(db, request, tokens):
    """Build the package for the current source state and record the delivery.

    Re-running with an unchanged source fingerprint returns the already
    delivered package; a changed source produces the next version while the
    previously delivered bytes stay untouched and are marked superseded.
    """
    content = build_content(db, request['job'], tokens)
    content_sha = digest(canon(content))
    fingerprint = digest(canon({
        'request': [request['event_id'], request['requester'], request['job'], request['recipient'],
                    request['scope'], request['signed_by'], request['valid_from'], request['valid_to']],
        'tokens': sorted(tokens), 'content_sha': content_sha}))
    last = db.execute('SELECT * FROM export_delivery WHERE request_id=? ORDER BY version DESC',
                      (request['event_id'],)).fetchone()
    if last and last['fingerprint'] == fingerprint:
        return json.loads(last['body']), False
    version = last['version'] + 1 if last else 1
    package = {'package_id': f"{request['event_id']}#v{version}", 'request_id': request['event_id'],
               'version': version, 'requester': request['requester'], 'recipient': request['recipient'],
               'job': request['job'], 'scope': request['scope'], **content,
               'manifest': {**content_stats(content['records']), 'sha': content_sha}}
    body = canon(package)
    if last:
        db.execute('UPDATE export_delivery SET superseded=1 WHERE request_id=?', (request['event_id'],))
    reason = 'package delivered' if version == 1 else f'reissue supersedes v{version - 1}: source state changed'
    db.execute('INSERT INTO export_delivery VALUES(?,?,?,?,?,?,?,?,?,0)',
               (request['event_id'], version, request['recipient'], request['job'], 'delivered',
                reason, digest(body), fingerprint, body))
    return package, True


def withdrawn(db, request_id):
    row = db.execute('SELECT 1 FROM request_withdrawal w JOIN data_request r ON r.event_id=w.target '
                     'WHERE w.target=? AND w.signed_by=r.requester', (request_id,)).fetchone()
    return bool(row)


def gate(db, request):
    """None when a package may be built now, else the (state, reason) hold."""
    if request['signed_by'] != request['requester']:
        return 'review', 'requester signature missing'
    tokens, error = parse_scope(request)
    if error:
        return 'review', error
    if request['recipient'] != request['requester']:
        if not request['valid_to']:
            return 'review', 'delegation validity missing'
        now = logical_now(db)
        if now and request['valid_from'] and now < request['valid_from']:
            return 'review', 'delegation not yet valid'
        if now and request['valid_to'] <= now:
            return 'blocked', 'delegation validity expired'
    return None


def decide(db, request_id):
    request = db.execute('SELECT * FROM data_request WHERE event_id=?', (request_id,)).fetchone()
    count = 0
    if not request:
        state, reason = 'pending', 'request missing'
    elif withdrawn(db, request_id):
        state, reason = 'blocked', 'authorization withdrawn by requester'
    else:
        hold = gate(db, request)
        if hold:
            state, reason = hold
        else:
            tokens, _ = parse_scope(request)
            package, created = deliver(db, request, tokens)
            count = package['manifest']['records']
            state = 'ready'
            if created:
                reason = f"package v{package['version']} delivered with {count} records"
            else:
                reason = f"package v{package['version']} unchanged; delivery already recorded"
    db.execute('INSERT INTO export_package VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,record_count=excluded.record_count',
               (request_id, state, reason, count))
    return dict(db.execute('SELECT * FROM export_package WHERE request_id=?', (request_id,)).fetchone())


def build_requests(db, request_ids=None):
    with db:
        if not request_ids:
            request_ids = [row['event_id'] for row in db.execute('SELECT event_id FROM data_request ORDER BY event_id')]
        return [decide(db, request_id) for request_id in sorted(request_ids)]


def pointer_get(body, pointer):
    value = body
    for part in pointer.lstrip('/').split('/'):
        value = value[part]
    return value


def source_value(db, evidence, job, source, field):
    if 'event_id' in source:
        row = evidence.get(source['event_id'])
        if not row:
            raise ValueError(f"provenance event missing: {source['event_id']}")
        return pointer_get(json.loads(row['body']), source['pointer'])
    history = db.execute('SELECT * FROM decision_history WHERE subsystem=? AND job=? AND version=?',
                         (source['decision'], job, source['version'])).fetchone()
    if not history:
        raise ValueError(f"decision provenance missing: {source['decision']} v{source['version']}")
    return history[field]


def verify(db, package):
    """Offline verification of a delivered package against the original database."""
    checks = []
    content = {key: package[key] for key in ('created_from', 'records', 'redactions', 'pending')}
    if digest(canon(content)) != package['manifest']['sha']:
        raise ValueError('manifest integrity failed')
    for key, value in content_stats(package['records']).items():
        if package['manifest'].get(key) != value:
            raise ValueError(f'manifest stat mismatch: {key}')
    checks.append('manifest-integrity')

    request = db.execute('SELECT * FROM data_request WHERE event_id=?', (package['request_id'],)).fetchone()
    if not request:
        raise ValueError('unknown request')
    for field in ('requester', 'recipient', 'job', 'scope'):
        if request[field] != package[field]:
            raise ValueError(f'package does not match request: {field}')
    if package['package_id'] != f"{package['request_id']}#v{package['version']}":
        raise ValueError('package id mismatch')
    tokens, error = parse_scope(request)
    if error:
        raise ValueError(f'request scope invalid: {error}')
    checks.append('request-match')

    others = other_job_tokens(db, request['job'])
    evidence = {row['event_id']: row for row in db.execute('SELECT event_id,kind,sha,body FROM evidence')}
    used_events, used_decisions = set(), set()
    for record in package['records']:
        kind = record['kind']
        if kind not in FIELD_SCOPE:
            raise ValueError(f'unknown record kind: {kind}')
        for field, cell in record['fields'].items():
            token = None if kind == 'certificate' else FIELD_SCOPE[kind].get(field)
            if token is None and kind != 'certificate':
                raise ValueError(f'unknown field policy: {kind}.{field}')
            if token is None or token not in tokens:
                if cell['status'] != 'redacted' or 'value' in cell:
                    raise ValueError(f"{record['record']}.{field} must stay redacted without a value")
                continue
            if cell['status'] == 'disclosed':
                actual = source_value(db, evidence, request['job'], cell['source'], field)
                if actual != cell['value']:
                    raise ValueError(f"{record['record']}.{field} does not match its source")
                if mixes_other_job(cell['value'], others):
                    raise ValueError(f"{record['record']}.{field} leaks other-job identifiers")
                if 'event_id' in cell['source']:
                    used_events.add(cell['source']['event_id'])
                else:
                    used_decisions.add((cell['source']['decision'], cell['source']['version']))
            elif cell['status'] == 'blocked':
                locator = [item for item in package['pending']
                           if item['record'] == record['record'] and item['field'] == field]
                if not locator:
                    raise ValueError(f"{record['record']}.{field} blocked without a pending locator")
                value = source_value(db, evidence, request['job'], locator[0]['source'], field)
                if not mixes_other_job(value, others):
                    raise ValueError(f"{record['record']}.{field} blocking is not justified")
                if 'event_id' in locator[0]['source']:
                    used_events.add(locator[0]['source']['event_id'])
            else:
                raise ValueError(f"unknown cell status: {cell['status']}")
    masked = {(record['record'], field) for record in package['records']
              for field, cell in record['fields'].items() if cell['status'] in ('redacted', 'blocked')}
    if masked != {(item['record'], item['field']) for item in package['redactions']}:
        raise ValueError('redaction list does not match masked fields')
    blocked = {(record['record'], field) for record in package['records']
               for field, cell in record['fields'].items() if cell['status'] == 'blocked'}
    if blocked != {(item['record'], item['field']) for item in package['pending']}:
        raise ValueError('pending locators do not match blocked fields')
    checks.append('field-provenance')

    anchored = {item['event_id'] for item in package['created_from']['evidence']}
    if anchored != used_events:
        raise ValueError('evidence anchors do not match disclosed sources')
    for item in package['created_from']['evidence']:
        row = evidence.get(item['event_id'])
        if not row or row['sha'] != item['sha'] or row['kind'] != item['kind']:
            raise ValueError(f"evidence anchor mismatch: {item['event_id']}")
    anchored_decisions = {(item['subsystem'], item['version']) for item in package['created_from']['decisions']}
    if anchored_decisions != used_decisions:
        raise ValueError('decision anchors do not match disclosed decisions')
    for item in package['created_from']['decisions']:
        row = db.execute('SELECT 1 FROM decision_history WHERE subsystem=? AND job=? AND version=? AND state=? AND reason=?',
                         (item['subsystem'], item['job'], item['version'], item['state'], item['reason'])).fetchone()
        if not row:
            raise ValueError(f"decision anchor mismatch: {item['subsystem']} v{item['version']}")
    checks.append('source-anchors')

    text = canon(package)
    for row in db.execute('SELECT raw FROM file_journal'):
        if row['raw'].decode('utf8') in text:
            raise ValueError('package embeds a raw source file')
    for row in db.execute('SELECT body FROM evidence'):
        if row['body'] in text:
            raise ValueError('package embeds an evidence body')
    for token in others:
        if token in text:
            raise ValueError(f'package leaks other-job identifier: {token}')
    checks.append('non-leakage')

    delivery = db.execute('SELECT * FROM export_delivery WHERE request_id=? AND version=?',
                          (package['request_id'], package['version'])).fetchone()
    if not delivery or delivery['sha'] != digest(text):
        raise ValueError('delivered bytes do not match this package')
    checks.append('delivery-record')
    return {'status': 'verified', 'package_id': package['package_id'], 'checks': checks,
            'superseded': bool(delivery['superseded'])}
