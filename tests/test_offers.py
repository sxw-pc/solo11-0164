import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.store import connect


class OfferCase(unittest.TestCase):
    def test_offer_collection_and_quote(self):
        raw = (Path(__file__).resolve().parents[1] / 'fixtures' / 'offer_ok.json').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / 'state.sqlite')
            try:
                result = run_import(db, 'offer-import', raw)
                self.assertEqual(result[0]['price'], 120)
                self.assertEqual(audit(db)['status'], 'matched')
            finally:
                db.close()
