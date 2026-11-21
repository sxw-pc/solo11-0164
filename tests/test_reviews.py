import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.store import connect


class ReviewCase(unittest.TestCase):
    def test_review_collection_and_quality(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'review_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                result = run_import(db, 'review-import', raw)
                self.assertEqual(result[0]['rating'], 2)
                self.assertEqual(db.execute('SELECT state FROM quality_case WHERE job=?', ('job-1',)).fetchone()[0], 'review')
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
