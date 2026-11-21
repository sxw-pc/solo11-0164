"""Baseline exam decision from the latest scanned answer sheet."""

import json


def decide(db, candidate, exam):
    key = db.execute('SELECT * FROM exam_key WHERE exam=?', (exam,)).fetchone()
    scan = db.execute('SELECT * FROM answer_scan WHERE candidate=? AND exam=? ORDER BY scanned_at DESC,event_id LIMIT 1',
                      (candidate, exam)).fetchone()
    if not key or not scan:
        state, score, source = 'pending', 0, None
    else:
        answers = json.loads(scan['answers'])
        expected = json.loads(key['answers'])
        score = sum(answers.get(question) == value for question, value in expected.items())
        state = 'passed' if score >= key['pass_mark'] else 'failed'
        source = scan['event_id']
    db.execute('INSERT INTO exam_result VALUES(?,?,?,?,?) ON CONFLICT(candidate,exam) DO UPDATE SET state=excluded.state,score=excluded.score,source_event=excluded.source_event',
               (candidate, exam, state, score, source))
    return dict(db.execute('SELECT * FROM exam_result WHERE candidate=? AND exam=?', (candidate, exam)).fetchone())
