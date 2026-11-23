"""Paper-level exam decision.

A physical answer sheet (paper_id) carries its own identity independent of the
candidate name printed on the card.  Version identity is built only from
physical-chain evidence (QR fragments + double-signed bag handoffs, or
per-paper issue records + a fully signed seating chain).  Names never identify
paper; repeat scans of one paper stay one paper; mutually exclusive proofs keep
the grade pending instead of picking a convenient version.
"""

import json

PENDING = 'pending_review'
PASSED = 'passed'
FAILED = 'failed'


def _load_versions(db, exam):
    return {row['version']: {'map': json.loads(row['question_map']), 'pass_mark': row['pass_mark']}
            for row in db.execute('SELECT * FROM exam_version WHERE exam=?', (exam,))}


def _legacy_keys(db):
    return {row['exam']: {'answers': json.loads(row['answers']), 'pass_mark': row['pass_mark']}
            for row in db.execute('SELECT * FROM exam_key')}


def list_papers(db):
    papers = {}
    for row in db.execute('SELECT paper_id,exam,candidate FROM sheet_scan'):
        papers.setdefault(row['paper_id'], {'exam': row['exam'], 'candidate': row['candidate']})
    for exam, key in _legacy_keys(db).items():
        for row in db.execute('SELECT * FROM answer_scan WHERE exam=? ORDER BY event_id', (exam,)):
            # Legacy single-key import has no paper id: each card event is its own paper.
            papers.setdefault(f'legacy:{row["event_id"]}', {'exam': exam, 'candidate': row['candidate'], 'legacy': row})
    return papers


def _bag_signers(db, bag_id):
    return {r['signer_a'] for r in db.execute('SELECT signer_a FROM bag_handoff WHERE bag_id=?', (bag_id,))} | \
           {r['signer_b'] for r in db.execute('SELECT signer_b FROM bag_handoff WHERE bag_id=?', (bag_id,))}


def _bag_paper_count(db, bag_id):
    return db.execute('SELECT COUNT(DISTINCT paper_id) FROM bag_handoff WHERE bag_id=? AND paper_id IS NOT NULL',
                      (bag_id,)).fetchone()[0]


def _valid_moves(db, exam):
    """Seat moves still in force after late signed corrections."""
    voided = {r['move_event_id'] for r in
              db.execute('SELECT move_event_id FROM seat_move_correction WHERE exam=? AND valid=0', (exam,))}
    return [r for r in db.execute('SELECT * FROM seat_move WHERE exam=? ORDER BY moved_at,event_id', (exam,))
            if r['event_id'] not in voided]


def _seat_occupancy(db, exam, room, at):
    """Reconstruct who sat in each seat at time at from charts and signed moves.

    A seat move without two distinct signatures, or one whose from-seat does not
    hold the moving candidate, breaks the chain: callers must not treat the
    resulting seating as proven.
    """
    seats, broken = {}, False
    charts = db.execute('SELECT * FROM seat_chart WHERE exam=? AND effective_at<=? ORDER BY effective_at,event_id',
                        (exam, at)).fetchall()
    for row in charts:
        if room is None or row['room'] == room:
            seats[row['seat_label']] = row['candidate']
    for move in _valid_moves(db, exam):
        if move['moved_at'] > at:
            break
        if move['room'] != room:
            continue
        if not move['signer_a'] or not move['signer_b'] or move['signer_a'] == move['signer_b']:
            broken = True
            continue
        if seats.get(move['from_seat']) != move['candidate']:
            broken = True
            continue
        seats[move['from_seat']] = None
        seats[move['to_seat']] = move['candidate']
    return seats, broken


def _bag_chain_ok(db, bag_id, paper_id):
    bag = db.execute('SELECT * FROM sealed_bag WHERE bag_id=?', (bag_id,)).fetchone()
    if not bag:
        return False, 'bag not found'
    if _bag_paper_count(db, bag_id) > 1:
        return False, 'one sealed-bag chain points at multiple physical papers'
    handoffs = db.execute('SELECT * FROM bag_handoff WHERE bag_id=? ORDER BY handed_at,event_id', (bag_id,)).fetchall()
    if not handoffs or any(r['paper_id'] not in (None, paper_id) for r in handoffs):
        return False, 'bag chain does not stay with this paper'
    if any(not r['signer_a'] or not r['signer_b'] or r['signer_a'] == r['signer_b'] for r in handoffs):
        return False, 'bag handoff lacks two distinct invigilator signatures'
    signers = _bag_signers(db, bag_id)
    if len(signers) < 2:
        return False, 'bag lacks two independent invigilator signatures'
    return True, bag['version']


def _issue_proof(db, paper_id, exam, candidate):
    issue = db.execute('SELECT * FROM sheet_issue WHERE paper_id=?', (paper_id,)).fetchone()
    if not issue:
        return None, 'no per-paper issue record'
    if not issue['signer_a'] or not issue['signer_b'] or issue['signer_a'] == issue['signer_b']:
        return None, 'issue record lacks two distinct signatures'
    occupancy, broken = _seat_occupancy(db, exam, issue['room'], issue['issued_at'])
    if broken:
        return None, 'seating chain contains an unsigned or inconsistent seat move'
    if occupancy.get(issue['seat_label']) != candidate:
        return None, 'signed seating chain does not place candidate on issued seat'
    ok, detail = _bag_chain_ok(db, issue['bag_id'], paper_id)
    if not ok:
        return None, f'issue bag chain invalid: {detail}'
    bag = db.execute('SELECT version FROM sealed_bag WHERE bag_id=?', (issue['bag_id'],)).fetchone()
    return {'version': bag['version'], 'bag_id': issue['bag_id'], 'issue_event': issue['event_id']}, None


def _paper_events_at(db, paper_id):
    stamps = [r['scanned_at'] for r in db.execute('SELECT scanned_at FROM sheet_scan WHERE paper_id=?', (paper_id,))]
    stamps += [r[0] for r in db.execute('SELECT recovered_at FROM qr_fragment WHERE paper_id=?', (paper_id,))]
    stamps += [r[0] for r in db.execute('SELECT issued_at FROM sheet_issue WHERE paper_id=?', (paper_id,))]
    stamps += [r[0] for r in db.execute(
        'SELECT h.handed_at FROM bag_handoff h JOIN sheet_issue i ON i.bag_id=h.bag_id WHERE i.paper_id=?',
        (paper_id,))]
    stamps += [r[0] for r in db.execute(
        'SELECT h.handed_at FROM bag_handoff h WHERE h.paper_id=?', (paper_id,))]
    # Seating evidence names the candidate rather than the paper; take later seat/correction events
    # of the bound candidate so a late correction always moves the paper clock forward.
    candidate = db.execute('SELECT candidate FROM sheet_scan WHERE paper_id=? LIMIT 1', (paper_id,)).fetchone()
    if candidate:
        stamps += [r[0] for r in db.execute(
            'SELECT m.moved_at FROM seat_move m WHERE m.candidate=?', (candidate[0],))]
        stamps += [r[0] for r in db.execute(
            'SELECT c.corrected_at FROM seat_move_correction c WHERE c.exam='
            '(SELECT exam FROM sheet_scan WHERE paper_id=? LIMIT 1)', (paper_id,))]
    return max(stamps) if stamps else None


def resolve_paper(db, paper_id):
    meta = list_papers(db)[paper_id]
    exam, candidate = meta['exam'], meta['candidate']
    versions = _load_versions(db, exam)
    scans = db.execute('SELECT * FROM sheet_scan WHERE paper_id=? ORDER BY scanned_at,event_id', (paper_id,)).fetchall()
    hard_conflicts, soft_notes, claims = [], [], {}

    # Legacy single-order exams: the sole key defines the version implicitly.
    if 'legacy' in meta and not versions:
        return _decide_legacy(db, meta['legacy'], _legacy_keys(db).get(exam))

    if not scans:
        hard_conflicts.append(('scan_missing', 'no physical sheet scan registered'))

    answer_sets = {r['answers'] for r in scans}
    if len(scans) > 1:
        if len(answer_sets) == 1:
            soft_notes.append(('repeat_scan', f'{len(scans)} scans of the same paper treated as one attempt'))
        else:
            hard_conflicts.append(('rescan_mismatch', 'same paper rescanned with mutually different answers'))

    # Hard conflict: one sealed-bag handoff chain points at two physical papers.
    for row in db.execute(
            "SELECT bag_id,COUNT(DISTINCT paper_id) AS n FROM bag_handoff "
            "WHERE paper_id IS NOT NULL AND paper_id<>'' GROUP BY bag_id"):
        if row['n'] <= 1:
            continue
        touched = {r['paper_id'] for r in db.execute(
            'SELECT DISTINCT paper_id FROM bag_handoff WHERE bag_id=? AND paper_id IS NOT NULL',
            (row['bag_id'],))}
        if paper_id in touched:
            hard_conflicts.append(('shared_bag_handoff',
                                   f'bag {row["bag_id"]} handoff names multiple papers: {sorted(touched)}'))

    # Route A: QR fragment on the paper + double-signed bag chain for that bag.
    for frag in db.execute('SELECT * FROM qr_fragment WHERE paper_id=? ORDER BY recovered_at,event_id', (paper_id,)):
        if frag['exam'] != exam or frag['version'] not in versions:
            soft_notes.append(('qr_ignored', f'fragment {frag["event_id"]} references unknown exam/version'))
            continue
        if frag['bag_id'] is None:
            soft_notes.append(('qr_ignored', f'fragment {frag["event_id"]} has no bag chain reference'))
            continue
        ok, detail = _bag_chain_ok(db, frag['bag_id'], paper_id)
        if not ok:
            soft_notes.append(('qr_ignored', f'{frag["event_id"]}: {detail}'))
            continue
        claims.setdefault(frag['version'], []).append(
            f'QR fragment {frag["event_id"]} + double-signed bag {frag["bag_id"]}')

    # Route B: per-paper issue record + complete dual-signed seating chain.
    proof, why = _issue_proof(db, paper_id, exam, candidate)
    if proof:
        claims.setdefault(proof['version'], []).append(
            f'per-paper issue {proof["issue_event"]} from bag {proof["bag_id"]} + signed seating chain')
    elif issue := db.execute('SELECT 1 FROM sheet_issue WHERE paper_id=?', (paper_id,)).fetchone():
        soft_notes.append(('issue_unproven', why))

    if len(claims) > 1:
        hard_conflicts.append(('exclusive_version_proofs',
                               '; '.join(f'v{v} via {chains}' for v, chains in sorted(claims.items()))))

    db.execute('DELETE FROM paper_conflict WHERE paper_id=?', (paper_id,))
    detected_at = _paper_events_at(db, paper_id) or (scans[-1]['scanned_at'] if scans else None)
    for kind, detail in hard_conflicts + soft_notes:
        db.execute('INSERT OR IGNORE INTO paper_conflict VALUES(?,?,?,?,?)',
                   (paper_id, exam, kind, detail, detected_at))

    if hard_conflicts:
        return _persist(db, paper_id, exam, candidate, None, PENDING, 'physical evidence conflict',
                        '; '.join(d for _, d in hard_conflicts), scans)

    if not claims:
        return _persist(db, paper_id, exam, candidate, None, PENDING, 'paper version unverified',
                        '; '.join(d for _, d in soft_notes) or 'no complete physical proof chain', scans)

    version = sorted(claims)[0]
    return _grade(db, paper_id, exam, candidate, version, claims[version][0], scans)


def _grade(db, paper_id, exam, candidate, version, basis, scans):
    vmeta = _load_versions(db, exam)[version]
    scan = scans[0]  # earliest scan is the attempt; later scans are duplicates, not new exams
    card = json.loads(scan['answers'])
    qmap = vmeta['map']
    mapped = {knowledge: card.get(position) for position, knowledge in qmap.items()}
    expected = {r['question']: r['answer'] for r in db.execute(
        'SELECT question,answer FROM knowledge_key WHERE exam=?', (exam,))}
    score = sum(mapped.get(q) == a for q, a in expected.items())
    state = PASSED if score >= vmeta['pass_mark'] else FAILED
    reason = f'mapped via version {version} question order against formal answer key'
    return _persist(db, paper_id, exam, candidate, version, state, basis, reason, scans,
                    score=score, pass_mark=vmeta['pass_mark'])


def _persist(db, paper_id, exam, candidate, version, state, basis, reason, scans, score=0, pass_mark=None):
    updated = _paper_events_at(db, paper_id) or (scans[-1]['scanned_at'] if scans else None)
    db.execute('''INSERT INTO paper_binding VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(paper_id) DO UPDATE SET exam=excluded.exam,candidate=excluded.candidate,
                  version=excluded.version,state=excluded.state,basis=excluded.basis,
                  reason=excluded.reason,updated_at=excluded.updated_at''',
               (paper_id, exam, candidate, version, state, basis, reason, updated))
    source = scans[0]['event_id'] if scans else None
    db.execute('''INSERT INTO exam_result VALUES(?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(paper_id) DO UPDATE SET exam=excluded.exam,candidate=excluded.candidate,
                  version=excluded.version,state=excluded.state,score=excluded.score,
                  pass_mark=excluded.pass_mark,source_event=excluded.source_event,reason=excluded.reason''',
               (paper_id, exam, candidate, version, state, score, pass_mark, source, reason))
    _sync_notices(db, paper_id, exam, candidate, state, score, updated)
    return dict(db.execute('SELECT * FROM exam_result WHERE paper_id=?', (paper_id,)).fetchone())


def _sync_notices(db, paper_id, exam, candidate, state, score, at):
    """Publish an immutable result notice; re-grade produces a correction, never a rewrite."""
    system = db.execute(
        "SELECT * FROM grade_notice WHERE paper_id=? AND kind IN ('result','correction') ORDER BY sent_at,notice_id",
        (paper_id,)).fetchall()
    latest = system[-1] if system else None
    if latest:
        try:
            payload = json.loads(latest['body'])
            same = payload.get('state') == state and payload.get('score') == score
        except (ValueError, TypeError):
            same = state in latest['body']
        if same:
            return
        kind, replaces = 'correction', latest['notice_id']
        # A re-grade may share the original scan's physical-event timestamp.  Keep the notice
        # clock strictly monotonic with a deterministic suffix so history order survives replay.
        seq = db.execute("SELECT COUNT(*) FROM grade_notice WHERE paper_id=?", (paper_id,)).fetchone()[0]
        sent_at = at if at > latest['sent_at'] else f"{at}#regrade{seq}"
        nid = f'notice-correction:{paper_id}:{seq}'
    else:
        kind, replaces, nid, sent_at = 'result', None, f'notice-result:{paper_id}', at
    body = json.dumps({'paper_id': paper_id, 'candidate': candidate, 'exam': exam,
                       'state': state, 'score': score, 'supersedes': replaces}, ensure_ascii=False, sort_keys=True)
    db.execute('INSERT OR IGNORE INTO grade_notice VALUES(?,?,?,?,?,?,?,?)',
               (nid, paper_id, exam, candidate, kind, body, sent_at, replaces))


def _decide_legacy(db, scan, key):
    paper_id = f'legacy:{scan["event_id"]}'
    if not key:
        return _persist_legacy(db, paper_id, scan, None, PENDING, 0, None, 'formal answer key missing')
    answers = json.loads(scan['answers'])
    score = sum(answers.get(q) == v for q, v in key['answers'].items())
    state = PASSED if score >= key['pass_mark'] else FAILED
    return _persist_legacy(db, paper_id, scan, 'legacy-single-order', state, score, key['pass_mark'],
                           'graded against sole published answer key')


def _persist_legacy(db, paper_id, scan, version, state, score, pass_mark, reason):
    db.execute('''INSERT INTO paper_binding VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(paper_id) DO UPDATE SET state=excluded.state,reason=excluded.reason''',
               (paper_id, scan['exam'], scan['candidate'], version, state, 'legacy answer_scan', reason,
                scan['scanned_at']))
    db.execute('''INSERT INTO exam_result VALUES(?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(paper_id) DO UPDATE SET state=excluded.state,score=excluded.score,
                  pass_mark=excluded.pass_mark,reason=excluded.reason''',
               (paper_id, scan['exam'], scan['candidate'], version, state, score, pass_mark,
                scan['event_id'], reason))
    return dict(db.execute('SELECT * FROM exam_result WHERE paper_id=?', (paper_id,)).fetchone())


def decide(db, candidate, exam):
    """Back-compat entry: re-grade every paper of the candidate/exam and return the latest."""
    rows = [resolve_paper(db, pid) for pid, meta in list_papers(db).items()
            if meta['exam'] == exam and meta['candidate'] == candidate]
    if not rows:
        return None
    return sorted(rows, key=lambda r: (r['source_event'] or '', r['paper_id']))[-1]


def recompute(db, exams=None):
    out = []
    targets = exams or {meta['exam'] for meta in list_papers(db).values()}
    for pid, meta in sorted(list_papers(db).items()):
        if meta['exam'] in targets:
            out.append(resolve_paper(db, pid))
    return out
