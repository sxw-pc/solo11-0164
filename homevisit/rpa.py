"""Offline insurance-portal RPA: persisted intents, scripted portal, safe recovery.

Every submission is first persisted as a submit_intent carrying the source
notice event, a content digest and a unique operation number, so a timed-out
request is recovered by querying the portal for that operation instead of
blindly opening a second case. The synthetic portal is a deterministic
stand-in driven entirely by fixture scripts, so audit replays reproduce the
same bindings from the journaled files.
"""

import hashlib
import json

from . import portal as portal_mod


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class SyntheticPortal:
    """Deterministic stand-in for the insurer web portal, driven by a fixture script."""

    def __init__(self, script):
        self.script = script or {}

    def submit(self, intent):
        specs = self.script.get('submit', {})
        if intent['operation'] not in specs:
            raise ValueError(f"portal script has no submit response for {intent['operation']}")
        return {'action': 'submit', 'operation': intent['operation'], **specs[intent['operation']]}

    def query(self, operation):
        specs = self.script.get('query', {})
        if operation not in specs:
            raise ValueError(f'portal script has no query response for {operation}')
        return {'action': 'query', 'operation': operation, **specs[operation]}


def get_intent(db, operation):
    row = db.execute('SELECT * FROM submit_intent WHERE operation=?', (operation,)).fetchone()
    return dict(row) if row else None


def _digest(notice, kind, amount, target_case):
    payload = {'notice': dict(notice), 'kind': kind, 'amount': amount, 'target_case': target_case}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def plan(db, operation, claim_id, kind, amount, target_case=None, at=''):
    """Persist the submission intent before any portal call; idempotent per operation."""
    notice = db.execute('SELECT * FROM insurance_notice WHERE claim_id=? ORDER BY event_id DESC LIMIT 1', (claim_id,)).fetchone()
    if not notice:
        raise ValueError(f'notice missing for {claim_id}')
    digest = _digest(notice, kind, amount, target_case)
    existing = get_intent(db, operation)
    if existing:
        same = (existing['claim_id'] == claim_id and existing['kind'] == kind and existing['amount'] == amount
                and existing['target_case'] == target_case and existing['digest'] == digest)
        if not same:
            raise ValueError(f'operation conflict: {operation}')
        return existing
    case = db.execute('SELECT state FROM insurance_case WHERE claim_id=?', (claim_id,)).fetchone()
    if case and case['state'] == 'disputed':
        raise ValueError(f'claim {claim_id} is disputed; manual resolution required')
    active = db.execute("SELECT * FROM case_binding WHERE claim_id=? AND state='active'", (claim_id,)).fetchall()
    if kind == 'submit':
        if active:
            raise ValueError(f"claim {claim_id} already has live case {active[0]['external_case']}; append or withdraw first")
    elif kind in ('append', 'withdraw'):
        if not target_case:
            raise ValueError(f'{kind} requires target_case')
        bound = db.execute("SELECT 1 FROM case_binding WHERE claim_id=? AND external_case=? AND state='active'",
                           (claim_id, target_case)).fetchone()
        if not bound:
            raise ValueError(f'no live case {target_case} for {claim_id}')
    else:
        raise ValueError(f'unknown intent kind {kind}')
    with db:
        db.execute('INSERT INTO submit_intent VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (operation, claim_id, notice['event_id'], kind, digest, amount, target_case, 'ready', at, None))
    return get_intent(db, operation)


def _exchange(db, operation, action, response):
    db.execute('INSERT INTO portal_exchange(operation,action,outcome,external_case,raw) VALUES(?,?,?,?,?)',
               (operation, action, response.get('result'), response.get('external_case'), _canonical(response)))


def _bind(db, intent, external):
    other = db.execute('SELECT * FROM case_binding WHERE external_case=?', (external,)).fetchone()
    if other:
        raise ValueError(f"portal case {external} already bound to {other['claim_id']}")
    db.execute('INSERT INTO case_binding VALUES(?,?,?,?,?,?)',
               (external, intent['claim_id'], intent['notice_event'], intent['operation'], intent['amount'], 'active'))


def execute(db, portal, operation):
    """Run one persisted intent against the portal; only 'ready' intents execute."""
    with db:
        intent = get_intent(db, operation)
        if not intent:
            raise ValueError(f'unknown operation {operation}')
        if intent['state'] != 'ready':
            return intent
        response = portal.submit(intent)
        _exchange(db, operation, 'submit', response)
        result = response.get('result')
        if result == 'timeout':
            state, external = 'unknown', None
        elif result == 'rejected':
            state, external = 'failed', None
        elif result == 'accepted':
            external = response['external_case']
            if intent['kind'] == 'submit':
                _bind(db, intent, external)
            elif external != intent['target_case']:
                raise ValueError(f"portal confirmed {external}, expected {intent['target_case']}")
            state = 'bound'
        else:
            raise ValueError(f'unexpected portal result {result}')
        db.execute('UPDATE submit_intent SET state=?, external_case=? WHERE operation=?', (state, external, operation))
        portal_mod.refresh_claims(db, [intent['claim_id']])
        return get_intent(db, operation)


def recover(db, portal, operation):
    """Resolve a timed-out intent by querying the portal for the operation number."""
    with db:
        intent = get_intent(db, operation)
        if not intent:
            raise ValueError(f'unknown operation {operation}')
        if intent['state'] != 'unknown':
            return intent
        response = portal.query(operation)
        _exchange(db, operation, 'query', response)
        result = response.get('result')
        if result == 'found':
            external = response['external_case']
            if intent['kind'] == 'submit':
                _bind(db, intent, external)
            elif external != intent['target_case']:
                raise ValueError(f"portal reported {external}, expected {intent['target_case']}")
            db.execute("UPDATE submit_intent SET state='bound', external_case=? WHERE operation=?", (external, operation))
        elif result == 'not_found':
            db.execute("UPDATE submit_intent SET state='ready' WHERE operation=?", (operation,))
        else:
            raise ValueError(f'unexpected portal result {result}')
        portal_mod.refresh_claims(db, [intent['claim_id']])
        return get_intent(db, operation)


def _emit_receipts(db, script):
    receipts = (script or {}).get('receipts') or []
    if receipts:
        portal_mod.import_data(db, {'receipts': receipts})


def run_file(db, data):
    """Plan then execute each listed intent, then ingest async portal receipts."""
    portal = SyntheticPortal(data.get('portal'))
    results = []
    for spec in data.get('intents', []):
        intent = plan(db, spec['operation'], spec['claim_id'], spec['kind'], spec.get('amount', 0),
                      spec.get('target_case'), spec.get('at', ''))
        results.append(execute(db, portal, intent['operation']))
    _emit_receipts(db, data.get('portal'))
    return results


def recover_file(db, data):
    """Query the portal for timed-out intents (listed or all unknown), then ingest receipts."""
    portal = SyntheticPortal(data.get('portal'))
    operations = data.get('recover')
    if not operations:
        operations = [r['operation'] for r in db.execute("SELECT operation FROM submit_intent WHERE state='unknown' ORDER BY operation")]
    results = [recover(db, portal, op) for op in operations]
    _emit_receipts(db, data.get('portal'))
    return results
