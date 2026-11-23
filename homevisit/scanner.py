"""Offline intake of synthetic marked answer-sheet scans and paper evidence.

``answer_scan`` keeps its original four fields (candidate/exam/scanned_at/
answers) for legacy single-order exams.  Physical-paper evidence — print
batches, sealed-bag handoffs with two invigilator signatures, per-paper issue
lists, seating charts and dual-signed seat moves, recovered QR fragments and
per-paper scans — lands in its own tables and drives identity resolution in
:mod:`homevisit.grading`.
"""

import json

from .source import record
from . import grading


def _insert_if_new(db, event_id, kind, row, sql, params):
    if record(db, event_id, kind, row):
        db.execute(sql, params)


def import_data(db, data):
    touched_exams = set()
    with db:
        # ---- legacy single-order answer keys (back-compat) ----
        for row in data.get('keys', []):
            db.execute(
                'INSERT INTO exam_key VALUES(?,?,?) ON CONFLICT(exam) DO UPDATE SET '
                'answers=excluded.answers,pass_mark=excluded.pass_mark',
                (row['exam'], json.dumps(row['answers'], sort_keys=True), row['pass_mark']))
            touched_exams.add(row['exam'])

        # ---- A/B versions: per-version question-order maps, formal keys, knowledge key ----
        for row in data.get('versions', []):
            db.execute(
                'INSERT INTO exam_version VALUES(?,?,?,?) ON CONFLICT(exam,version) DO UPDATE SET '
                'question_map=excluded.question_map,pass_mark=excluded.pass_mark',
                (row['exam'], row['version'], json.dumps(row['question_map'], sort_keys=True), row['pass_mark']))
            answers = row.get('answers', {})
            db.execute(
                'INSERT INTO exam_version_key VALUES(?,?,?) ON CONFLICT(exam,version) DO UPDATE SET '
                'answers=excluded.answers',
                (row['exam'], row['version'], json.dumps(answers, sort_keys=True)))
            touched_exams.add(row['exam'])
        for row in data.get('knowledge_keys', []):
            db.execute('INSERT OR REPLACE INTO knowledge_key VALUES(?,?,?)',
                       (row['exam'], row['question'], row['answer']))
            touched_exams.add(row['exam'])

        # ---- legacy card scans: only candidate/exam/time/answers are captured ----
        for row in data.get('scans', []):
            _insert_if_new(
                db, row['event_id'], 'answer_scan', row,
                'INSERT INTO answer_scan VALUES(?,?,?,?,?)',
                (row['event_id'], row['candidate'], row['exam'], row['scanned_at'],
                 json.dumps(row['answers'], sort_keys=True)))
            touched_exams.add(row['exam'])

        # ---- per-paper physical scans ----
        for row in data.get('sheet_scans', []):
            _insert_if_new(
                db, row['event_id'], 'sheet_scan', row,
                'INSERT INTO sheet_scan VALUES(?,?,?,?,?,?)',
                (row['event_id'], row['paper_id'], row['exam'], row['candidate'], row['scanned_at'],
                 json.dumps(row['answers'], sort_keys=True)))
            touched_exams.add(row['exam'])

        # ---- print batches / sealed bags ----
        for row in data.get('bags', []):
            db.execute('INSERT OR REPLACE INTO sealed_bag VALUES(?,?,?,?)',
                       (row['bag_id'], row['exam'], row['version'], row['sealed_at']))
            touched_exams.add(row['exam'])

        # ---- double-signed bag handoffs ----
        for row in data.get('handoffs', []):
            _insert_if_new(
                db, row['event_id'], 'bag_handoff', row,
                'INSERT INTO bag_handoff VALUES(?,?,?,?,?,?)',
                (row['event_id'], row['bag_id'], row.get('paper_id'), row['signer_a'],
                 row.get('signer_b'), row['handed_at']))
            bag = db.execute('SELECT exam FROM sealed_bag WHERE bag_id=?', (row['bag_id'],)).fetchone()
            if bag:
                touched_exams.add(bag['exam'])

        # ---- per-paper issue list inside a bag ----
        for row in data.get('issues', []):
            _insert_if_new(
                db, row['event_id'], 'sheet_issue', row,
                'INSERT INTO sheet_issue VALUES(?,?,?,?,?,?,?,?)',
                (row['event_id'], row['bag_id'], row['paper_id'], row.get('room'),
                 row['seat_label'], row['issued_at'], row['signer_a'], row.get('signer_b')))  # noqa: E501
            bag = db.execute('SELECT exam FROM sealed_bag WHERE bag_id=?', (row['bag_id'],)).fetchone()
            if bag:
                touched_exams.add(bag['exam'])

        # ---- seating charts (snapshots) ----
        for row in data.get('seat_charts', []):
            if record(db, row['event_id'], 'seat_chart', row):
                db.execute('INSERT INTO seat_chart VALUES(?,?,?,?,?,?)',
                           (row['event_id'], row['exam'], row['room'], row['seat_label'],
                            row['candidate'], row['effective_at']))
            touched_exams.add(row['exam'])

        # ---- seat moves: from- and to-seat both signed ----
        for row in data.get('seat_moves', []):
            _insert_if_new(
                db, row['event_id'], 'seat_move', row,
                'INSERT INTO seat_move VALUES(?,?,?,?,?,?,?,?,?)',
                (row['event_id'], row['exam'], row['candidate'], row['room'],
                 row['from_seat'], row['to_seat'], row['moved_at'],
                 row.get('signer_a'), row.get('signer_b')))  # noqa: E501
            touched_exams.add(row['exam'])

        # ---- late signed corrections of seat moves ----
        for row in data.get('seat_move_corrections', []):
            _insert_if_new(
                db, row['event_id'], 'seat_move_correction', row,
                'INSERT INTO seat_move_correction VALUES(?,?,?,?,?,?,?)',
                (row['event_id'], row['exam'], row['move_event_id'], int(row['valid']),
                 row['signer_a'], row['signer_b'], row['corrected_at']))  # noqa: E501
            touched_exams.add(row['exam'])

        # ---- QR fragments recovered from the physical paper ----
        for row in data.get('qr_fragments', []):
            _insert_if_new(
                db, row['event_id'], 'qr_fragment', row,
                'INSERT INTO qr_fragment VALUES(?,?,?,?,?,?)',
                (row['event_id'], row['paper_id'], row['exam'], row['version'],
                 row.get('bag_id'), row['recovered_at']))
            touched_exams.add(row['exam'])

        # ---- externally issued grade notices (immutable history) ----
        for row in data.get('notices', []):
            _insert_if_new(
                db, row['notice_id'], 'grade_notice', row,
                'INSERT INTO grade_notice VALUES(?,?,?,?,?,?,?,?)',
                (row['notice_id'], row['paper_id'], row['exam'], row['candidate'],
                 row['kind'], row['body'], row['sent_at'], row.get('replaces_notice')))
            touched_exams.add(row['exam'])

        return grading.recompute(db, touched_exams)
