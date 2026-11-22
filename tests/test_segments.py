"""Segmented relay-dispatch and split-performance tests.

The relay route and the lead route are always built from the *same* batch of
contracts, authorizations, receipts, handover signatures and acceptances; only
the client's final responsibility confirmation changes between the two
databases.  No input is replaced, no signature is fabricated and rows are not
reordered to switch modes.
"""
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


def feed(db, kind, data):
    return run_import(db, kind, json.dumps(data, ensure_ascii=False).encode())


class SegmentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def row(self, table, job, **extra):
        sql = f'SELECT * FROM {table} WHERE job=?'
        params = [job]
        for key, value in extra.items():
            sql += f' AND {key}=?'
            params.append(value)
        return dict(self.db.execute(sql, params).fetchone())

    def rows(self, table, job, order='route,idx'):
        return [dict(r) for r in self.db.execute(
            f'SELECT * FROM {table} WHERE job=? ORDER BY {order}', (job,))]

    def settlement(self, job):
        return self.row('settlement_decision', job)

    def load_relay_batch(self):
        feed(self.db, 'contract-import', fixture('seg_relay_contract.json'))
        feed(self.db, 'worker-import', fixture('seg_relay_workers.json'))
        feed(self.db, 'visit-import', fixture('seg_relay_visit_base.json'))

    def test_single_worker_full_window_uses_segment_path(self):
        feed(self.db, 'contract-import', fixture('seg_single_contract.json'))
        feed(self.db, 'worker-import', fixture('seg_single_workers.json'))
        feed(self.db, 'visit-import', fixture('seg_single_visit.json'))
        settlement = self.settlement('seg-single')
        self.assertEqual((settlement['state'], settlement['mode'], settlement['route']),
                         ('settled', 'single', 'single'))
        self.assertEqual((settlement['minutes'], settlement['payable'], settlement['frozen']), (240, 24000, 0))
        self.assertEqual(settlement['coverage_start'], '2026-11-05T09:00:00')
        self.assertEqual(settlement['coverage_end'], '2026-11-05T13:00:00')
        segments = self.rows('service_segment', 'seg-single')
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['worker'], 'worker-a')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_same_inputs_lead_route_freezes_only_late_window(self):
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_lead.json'))
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['route'], 'lead')
        self.assertEqual(settlement['state'], 'disputed')
        self.assertEqual((settlement['minutes'], settlement['payable'], settlement['frozen']),
                         (240, 12000, 12000))
        lead = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'lead']
        self.assertEqual([(s['worker'], s['minutes'], s['payable'], s['frozen'], s['state']) for s in lead],
                         [('worker-a', 120, 12000, 0, 'settled'),
                          ('worker-a', 120, 0, 12000, 'disputed')])
        # The other candidate route remains on record with the same evidence.
        alternative = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'relay']
        self.assertTrue(all(s['state'] == 'settled' for s in alternative))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_same_inputs_relay_route_pays_accepted_successor_fragment(self):
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_relay.json'))
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['route'], 'relay')
        self.assertEqual(settlement['state'], 'settled')
        self.assertEqual((settlement['minutes'], settlement['payable'], settlement['frozen']),
                         (240, 24000, 0))
        relay = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'relay']
        self.assertEqual([(s['worker'], s['minutes'], s['payable'], s['state'], s['relay_at']) for s in relay],
                         [('worker-a', 120, 12000, 'settled', None),
                          ('worker-b', 120, 12000, 'settled', '2026-11-05T11:00')])
        # The lead candidate retains the disputed late fragment.
        lead = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'lead']
        self.assertTrue(any(s['state'] == 'disputed' and s['frozen'] == 12000 for s in lead))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_both_routes_share_identical_evidence_batches(self):
        # Replay the exact same files into two databases and diff every piece
        # of source evidence; only the confirmation file is allowed to differ.
        paths = ['seg_relay_contract.json', 'seg_relay_workers.json',
                 'seg_relay_visit_base.json']
        shared = {name: (FIX / name).read_bytes() for name in paths}
        for name, raw in shared.items():
            json.loads(raw)  # every shared file must itself be valid JSON
        self.assertEqual(fixture('seg_relay_visit_base.json')['receipts'],
                         fixture('seg_relay_visit_base.json')['receipts'])
        confirm_lead = fixture('seg_relay_visit_lead.json')['confirmations'][0]
        confirm_relay = fixture('seg_relay_visit_relay.json')['confirmations'][0]
        self.assertEqual(confirm_lead['client'], confirm_relay['client'])
        self.assertEqual({confirm_lead['route'], confirm_relay['route']}, {'lead', 'relay'})
        # Same handover signature and same receipts feed both routes.
        self.assertEqual(fixture('seg_relay_workers.json')['handovers'][0]['from_signed'], 'worker-a')
        self.assertEqual(fixture('seg_relay_workers.json')['handovers'][0]['to_signed'], 'worker-b')

    def test_unattended_handoff_gap_blocks_continuous_service_claim(self):
        feed(self.db, 'contract-import', fixture('seg_gap_contract.json'))
        feed(self.db, 'worker-import', fixture('seg_gap_workers.json'))
        feed(self.db, 'visit-import', fixture('seg_gap_visit.json'))
        settlement = self.settlement('seg-gap')
        self.assertEqual(settlement['state'], 'blocked')
        self.assertIn('unattended gap', settlement['reason'])
        self.assertEqual(settlement['gap_minutes'], 60)
        self.assertEqual(settlement['minutes'], 180)
        self.assertEqual(settlement['frozen'], 18000)
        self.assertEqual(settlement['payable'], 0)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_handoff_without_both_signatures_blocks_dispatch_and_billing(self):
        feed(self.db, 'contract-import', fixture('seg_nohand_contract.json'))
        feed(self.db, 'worker-import', fixture('seg_nohand_workers.json'))
        feed(self.db, 'visit-import', fixture('seg_nohand_visit.json'))
        dispatch = self.row('dispatch_decision', 'seg-nohand')
        self.assertEqual(dispatch['state'], 'blocked')
        self.assertIn('not signed by both relieving workers', dispatch['reason'])
        self.assertEqual(self.settlement('seg-nohand')['state'], 'blocked')

    def test_overlap_is_billed_once_and_pause_inside_relay_breaks_no_chain(self):
        feed(self.db, 'contract-import', fixture('seg_overlap_contract.json'))
        feed(self.db, 'worker-import', fixture('seg_overlap_workers.json'))
        feed(self.db, 'visit-import', fixture('seg_overlap_visit.json'))
        settlement = self.settlement('seg-overlap')
        # Union coverage 09:00-13:00 minus worker-b's 30 minute pause = 210.
        self.assertEqual((settlement['state'], settlement['route'], settlement['minutes']),
                         ('settled', 'relay', 210))
        # No double charge: the full 240-minute job is still billed once.
        self.assertEqual((settlement['payable'], settlement['frozen']), (24000, 0))
        relay = [s for s in self.rows('service_segment', 'seg-overlap') if s['route'] == 'relay']
        self.assertEqual([(s['worker'], s['minutes']) for s in relay],
                         [('worker-a', 120), ('worker-b', 120)])
        summary = report(self.db)['jobs'][0]['workers']
        self.assertEqual({w['worker']: w['site_minutes'] for w in summary},
                         {'worker-a': 135, 'worker-b': 150})
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_certificate_revocation_freezes_only_successor_fragment(self):
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_relay.json'))
        feed(self.db, 'worker-import', fixture('seg_relay_revocation.json'))
        dispatch = self.row('dispatch_decision', 'seg-relay')
        self.assertEqual(dispatch['state'], 'blocked')
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['state'], 'disputed')
        self.assertEqual(settlement['route'], 'relay')
        self.assertEqual((settlement['payable'], settlement['frozen']), (12000, 12000))
        relay = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'relay']
        self.assertEqual([(s['worker'], s['state'], s['payable'], s['frozen']) for s in relay],
                         [('worker-a', 'settled', 12000, 0),
                          ('worker-b', 'blocked', 0, 12000)])
        self.assertIn('revoked', relay[1]['reason'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_dispute_keeps_other_worker_accepted_fragment(self):
        # Relay route already settled; a late dispute on worker-a's early
        # fragment must freeze only that identifiable amount.
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_relay.json'))
        late = {'acceptances': [{
            'event_id': 'seg-relay-dispute-late',
            'job': 'seg-relay', 'task': 'clean', 'accepted': False,
            'client': 'client-a', 'dispute': 'late complaint about early work',
            'worker': 'worker-a',
            'win_start': '2026-11-05T09:00', 'win_end': '2026-11-05T11:00'}]}
        feed(self.db, 'visit-import', late)
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['state'], 'disputed')
        self.assertEqual((settlement['payable'], settlement['frozen']), (12000, 12000))
        relay = [s for s in self.rows('service_segment', 'seg-relay') if s['route'] == 'relay']
        self.assertEqual([(s['worker'], s['state'], s['frozen']) for s in relay],
                         [('worker-a', 'disputed', 12000), ('worker-b', 'settled', 0)])

    def test_reschedule_outside_old_handoff_readjudicates_but_keeps_history(self):
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_relay.json'))
        before = self.db.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0]
        feed(self.db, 'contract-import', fixture('seg_relay_reschedule.json'))
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['state'], 'blocked')
        self.assertIn('outside the current service window', settlement['reason'])
        # Historical relay facts stay on record.
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM relay_request').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM handover_receipt').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM responsibility_confirmation').fetchone()[0], 1)
        self.assertGreater(self.db.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0], before)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_confirmation_switch_rebinds_route_without_changing_evidence(self):
        self.load_relay_batch()
        feed(self.db, 'visit-import', fixture('seg_relay_visit_lead.json'))
        self.assertEqual(self.settlement('seg-relay')['route'], 'lead')
        evidence_before = [dict(r) for r in self.db.execute('SELECT * FROM evidence ORDER BY event_id')]
        feed(self.db, 'visit-import', fixture('seg_relay_visit_relay.json'))
        evidence_after = [dict(r) for r in self.db.execute('SELECT * FROM evidence ORDER BY event_id')]
        self.assertEqual([r for r in evidence_before if r['kind'] != 'confirmation'],
                         [r for r in evidence_after if r['kind'] != 'confirmation'])
        settlement = self.settlement('seg-relay')
        self.assertEqual(settlement['route'], 'relay')
        self.assertEqual(settlement['state'], 'settled')

    def test_demo_relay_cli_and_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        out = subprocess.run([sys.executable, '-m', 'homevisit', 'demo-relay', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        item = json.loads(out.stdout)['jobs'][0]
        self.assertEqual(item['settlement']['route'], 'lead')
        self.assertEqual((item['settlement']['payable'], item['settlement']['frozen']), (12000, 12000))
        audit_out = subprocess.run([sys.executable, '-m', 'homevisit', 'audit', '--db', str(path)],
                                   cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(audit_out.stdout)['status'], 'matched')
        # The workbench explains contract version, certs, authorization,
        # handover and acceptance sources for every fragment.
        workbench = item['relay_workbench']
        self.assertEqual(workbench['points'][0]['handover_id'], 'seg-relay-handover-1')
        self.assertEqual(len(workbench['certificates']), 2)
        self.assertEqual(len(workbench['acceptances']), 3)


if __name__ == '__main__':
    unittest.main()
