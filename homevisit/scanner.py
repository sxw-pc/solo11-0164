"""Offline intake of synthetic marked answer-sheet scans."""

import json
from .source import record


def import_data(db, data):
    touched = set()
    with db:
        for row in data.get('keys', []):
            db.execute('INSERT INTO exam_key VALUES(?,?,?) ON CONFLICT(exam) DO UPDATE SET answers=excluded.answers,pass_mark=excluded.pass_mark',
                       (row['exam'], json.dumps(row['answers'], sort_keys=True), row['pass_mark']))
        for row in data.get('scans', []):
            if record(db, row['event_id'], 'answer_scan', row):
                db.execute('INSERT INTO answer_scan VALUES(?,?,?,?,?)',
                           (row['event_id'], row['candidate'], row['exam'], row['scanned_at'], json.dumps(row['answers'], sort_keys=True)))
            touched.add((row['candidate'], row['exam']))
        from .grading import decide
        return [decide(db, candidate, exam) for candidate, exam in sorted(touched)]
