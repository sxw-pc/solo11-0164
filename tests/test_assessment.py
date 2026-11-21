import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.store import connect


class AssessmentCase(unittest.TestCase):
    def test_exam_replay(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'exam_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                result = run_import(db, 'exam-import', raw)
                self.assertEqual(result[0]['state'], 'passed')
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
