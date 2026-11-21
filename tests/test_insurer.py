import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.store import connect


class InsuranceCase(unittest.TestCase):
    def test_insurer_portal_replay(self):
        root = Path(__file__).resolve().parents[1] / 'fixtures'
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                run_import(db, 'insurance-import', (root / 'insurance_ok.json').read_bytes())
                result = run_import(db, 'portal-import', (root / 'portal_ok.json').read_bytes())
                self.assertEqual(result[0]['state'], 'submitted')
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
