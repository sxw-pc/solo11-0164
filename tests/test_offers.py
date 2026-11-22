import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.store import connect
from homevisit.report import report

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


class OfferCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def feed(self, name):
        return run_import(self.db, 'offer-import', json.dumps(fixture(name), ensure_ascii=False).encode())

    def feed_data(self, data):
        return run_import(self.db, 'offer-import', json.dumps(data, ensure_ascii=False).encode())

    def quote(self, service='cleaning', region='city-east'):
        return self.db.execute('SELECT * FROM market_quote WHERE service=? AND region=?', (service, region)).fetchone()

    def versions(self, service='cleaning', region='city-east'):
        return self.db.execute('SELECT * FROM quote_version WHERE service=? AND region=? ORDER BY version',
                               (service, region)).fetchall()

    def test_offer_collection_and_quote(self):
        raw = (FIX / 'offer_ok.json').read_bytes()
        result = run_import(self.db, 'offer-import', raw)
        self.assertEqual(result[0]['price'], 120)
        self.assertEqual(result[0]['state'], 'quoted')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_r1_footnote_contradiction_stays_pending(self):
        self.feed('offer_r1.json')
        quote = self.quote()
        self.assertEqual(quote['state'], 'pending')
        self.assertIsNone(quote['amount'])
        self.assertNotIn(quote['amount'], (160, 200))
        with self.assertRaisesRegex(ValueError, 'not deliverable'):
            self.feed('offer_delivery.json')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM quote_delivery').fetchone()[0], 0)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_same_region_receipts_verify_200(self):
        self.feed('offer_r1.json')
        self.feed('offer_r1_receipts.json')
        quote = self.quote()
        self.assertEqual((quote['state'], quote['amount']), ('verified', 200))
        proofs = json.loads(quote['proofs'])
        self.assertEqual({p['event_id'] for p in proofs}, {'offer-r1', 'settle-r1', 'order-r1'})
        self.assertEqual([v['state'] for v in self.versions()], ['pending', 'verified'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_signed_errata_verify_160(self):
        self.feed('offer_r1.json')
        self.feed('offer_r1_errata.json')
        quote = self.quote()
        self.assertEqual((quote['state'], quote['amount']), ('verified', 160))
        proofs = json.loads(quote['proofs'])
        self.assertEqual({p['event_id'] for p in proofs}, {'offer-r1', 'errata-r1'})
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_cross_region_receipt_never_merges(self):
        self.feed('offer_r1.json')
        self.feed('offer_r1_cross_region.json')
        east = self.quote()
        self.assertEqual((east['state'], east['amount']), ('pending', None))
        west = self.quote(region='city-west')
        self.assertEqual((west['state'], west['amount']), ('pending', None))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_counter_proof_disputes_and_locates_recipients(self):
        self.feed('offer_r1.json')
        self.feed('offer_r1_receipts.json')
        self.feed('offer_delivery.json')
        self.assertEqual((self.quote()['state'], self.quote()['amount']), ('verified', 200))
        self.feed('offer_r1_counter.json')
        quote = self.quote()
        self.assertEqual(quote['state'], 'disputed')
        self.assertIsNone(quote['amount'])
        self.assertEqual([v['state'] for v in self.versions()], ['pending', 'verified', 'disputed'])
        east = [q for q in report(self.db)['market_quotes'] if q['region'] == 'city-east'][0]
        self.assertEqual([d['recipient'] for d in east['delivered_to']], ['client-a'])
        delivered = self.db.execute('SELECT * FROM quote_delivery WHERE event_id=?', ('deliver-1',)).fetchone()
        self.assertEqual((delivered['state'], delivered['amount'], delivered['quote_version']), ('verified', 200, 2))
        with self.assertRaisesRegex(ValueError, 'not deliverable'):
            data = fixture('offer_delivery.json')
            data['deliveries'][0].update(event_id='deliver-2', recipient='client-b')
            self.feed_data(data)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_retraction_expires_quote_and_blocks_reuse(self):
        self.feed('offer_r1.json')
        self.feed('offer_r1_errata.json')
        self.feed('offer_delivery.json')
        self.assertEqual((self.quote()['state'], self.quote()['amount']), ('verified', 160))
        self.feed('offer_r1_retract.json')
        self.assertEqual(self.quote()['state'], 'expired')
        with self.assertRaisesRegex(ValueError, 'not deliverable'):
            data = fixture('offer_delivery.json')
            data['deliveries'][0].update(event_id='deliver-3', recipient='client-c')
            self.feed_data(data)
        delivered = self.db.execute('SELECT * FROM quote_delivery WHERE event_id=?', ('deliver-1',)).fetchone()
        self.assertEqual((delivered['state'], delivered['amount']), ('verified', 160))
        terms = json.loads(delivered['terms'])
        self.assertEqual((terms['callout_fee'], terms['valid_to']), (40, '2026-12-01'))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_unsigned_errata_rejected(self):
        self.feed('offer_r1.json')
        data = fixture('offer_r1_errata.json')
        data['errata'][0]['signed_by'] = 'operator'
        with self.assertRaisesRegex(ValueError, 'signature'):
            self.feed_data(data)
        self.assertEqual(self.quote()['state'], 'pending')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_offer_cli_flow(self):
        path = Path(self.tmp.name) / 'cli.sqlite'

        def cli(*args):
            return subprocess.run([sys.executable, '-B', '-m', 'homevisit', *args, '--db', str(path)],
                                  cwd=ROOT, text=True, capture_output=True, check=True).stdout

        cli('offer-import', '--file', str(FIX / 'offer_r1.json'))
        cli('offer-import', '--file', str(FIX / 'offer_r1_receipts.json'))
        cli('offer-import', '--file', str(FIX / 'offer_delivery.json'))
        out = json.loads(cli('quote', '--service', 'cleaning', '--region', 'city-east'))
        self.assertEqual((out['quote']['state'], out['quote']['amount']), ('verified', 200))
        self.assertEqual([d['recipient'] for d in out['deliveries']], ['client-a'])
        self.assertEqual(json.loads(cli('audit'))['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
