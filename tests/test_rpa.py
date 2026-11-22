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
    return (FIX / name).read_bytes()


class PortalRpa(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def feed(self, kind, name):
        return run_import(self.db, kind, fixture(name))

    def feed_data(self, kind, data):
        return run_import(self.db, kind, json.dumps(data, ensure_ascii=False).encode())

    def claim(self, cid='claim-1'):
        return next(c for c in report(self.db)['insurance']['claims'] if c['claim_id'] == cid)

    def chain(self, cid='claim-1'):
        return [(c['external_case'], c['status']) for c in self.claim(cid)['case_chain']]

    def route_b_preamble(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_supplement.json')
        self.feed('rpa-run', 'rpa_withdraw.json')
        self.feed('portal-import', 'portal_withdrawn.json')

    def test_submit_binds_notice_to_external_case(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        c = self.claim()
        self.assertEqual((c['state'], c['external_case']), ('accepted', 'ext-1'))
        self.assertEqual([(b['external_case'], b['notice_event'], b['operation'], b['state']) for b in c['bindings']],
                         [('ext-1', 'insurance-notice-1', 'op-0001', 'active')])
        self.assertEqual((c['recoverable'], c['pending_review'], c['pending_queries']), (5000, 0, []))
        self.assertEqual(self.chain(), [('ext-1', 'accepted')])
        self.assertEqual(report(self.db)['insurance']['disputes'], [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_supplement_notice_keeps_submitted_case(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_supplement.json')
        c = self.claim()
        self.assertEqual(c['declared'], 6500)
        self.assertEqual((c['state'], c['external_case']), ('accepted', 'ext-1'))
        self.assertEqual([(b['external_case'], b['state']) for b in c['bindings']], [('ext-1', 'active')])
        self.assertEqual(c['recoverable'], 5000)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_append_route_keeps_single_case_with_pending_amount(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_supplement.json')
        self.feed('rpa-run', 'rpa_append.json')
        c = self.claim()
        self.assertEqual((c['state'], c['external_case']), ('under_review', 'ext-1'))
        self.assertEqual(self.chain(), [('ext-1', 'under_review')])
        self.assertEqual((c['recoverable'], c['pending_review']), (6500, 1500))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_withdraw_refile_route(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_supplement.json')
        self.feed('rpa-run', 'rpa_withdraw.json')
        # withdrawal requested but not yet effective: ext-1 still live, refile refused
        self.assertEqual(self.claim()['state'], 'accepted')
        with self.assertRaisesRegex(ValueError, 'append or withdraw first'):
            self.feed('rpa-run', 'rpa_refile.json')
        self.feed('portal-import', 'portal_withdrawn.json')
        c = self.claim()
        self.assertEqual((c['state'], c['recoverable']), ('withdrawn', 0))
        self.feed('rpa-run', 'rpa_refile.json')
        c = self.claim()
        self.assertEqual((c['state'], c['external_case']), ('accepted', 'ext-2'))
        self.assertEqual(self.chain(), [('ext-1', 'withdrawn'), ('ext-2', 'accepted')])
        self.assertEqual((c['recoverable'], c['pending_review']), (6500, 0))
        self.assertEqual([o['operation'] for o in c['operations']], ['op-0001', 'op-0003', 'op-0004'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_old_acceptance_cannot_reopen_withdrawn_case(self):
        self.route_b_preamble()
        self.feed('rpa-run', 'rpa_refile.json')
        self.feed('portal-import', 'portal_late_accept.json')
        c = self.claim()
        self.assertEqual((c['state'], c['external_case']), ('accepted', 'ext-2'))
        self.assertEqual(self.chain(), [('ext-1', 'withdrawn'), ('ext-2', 'accepted')])
        self.assertEqual(c['recoverable'], 6500)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_refile_timeout_recovers_existing_ext2_without_ext3(self):
        self.route_b_preamble()
        self.feed('rpa-run', 'rpa_refile_timeout.json')
        c = self.claim()
        self.assertEqual(c['pending_queries'], ['op-0004'])
        self.assertEqual(c['recoverable'], 0)
        self.assertEqual([b for b in c['bindings'] if b['state'] == 'active'], [])
        self.feed('rpa-recover', 'rpa_recover.json')
        c = self.claim()
        self.assertEqual([(b['external_case'], b['state']) for b in c['bindings']],
                         [('ext-1', 'withdrawn'), ('ext-2', 'active')])
        self.assertEqual(c['pending_queries'], [])
        self.assertEqual((c['recoverable'], c['pending_review']), (6500, 0))
        externals = [x['external_case'] for x in c['case_chain']]
        self.assertEqual(externals, ['ext-1', 'ext-2'])
        self.assertNotIn('ext-3', externals)
        self.assertEqual([o['operation'] for o in c['operations']], ['op-0001', 'op-0003', 'op-0004'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_submit_timeout_then_query_binds_portal_case(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_timeout.json')
        c = self.claim()
        self.assertEqual((c['state'], c['pending_queries']), ('ready', ['op-0001']))
        self.assertEqual((c['recoverable'], c['bindings']), (0, []))
        self.feed('rpa-recover', 'rpa_recover_basic.json')
        c = self.claim()
        self.assertEqual([(b['external_case'], b['state']) for b in c['bindings']], [('ext-2', 'active')])
        self.assertEqual((c['recoverable'], c['pending_review']), (5000, 0))
        self.assertEqual([x['external_case'] for x in c['case_chain']], ['ext-2'])
        self.assertEqual([o['operation'] for o in c['operations']], ['op-0001'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_timeout_not_found_resubmits_same_operation(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_timeout_lost.json')
        self.assertEqual(self.claim()['pending_queries'], ['op-0001'])
        self.feed('rpa-recover', 'rpa_recover_lost.json')
        self.assertEqual(self.claim()['operations'][0]['state'], 'ready')
        self.feed('rpa-run', 'rpa_resubmit.json')
        c = self.claim()
        self.assertEqual([(b['external_case'], b['state']) for b in c['bindings']], [('ext-2', 'active')])
        self.assertEqual([o['operation'] for o in c['operations']], ['op-0001'])
        self.assertEqual([(x['action'], x['outcome']) for x in c['exchanges']],
                         [('submit', 'timeout'), ('query', 'not_found'), ('submit', 'accepted')])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_rejected_operation_does_not_bind(self):
        self.feed('insurance-import', 'insurance_ok.json')
        data = {'intents': [{'operation': 'op-0001', 'claim_id': 'claim-1', 'kind': 'submit', 'amount': 5000}],
                'portal': {'submit': {'op-0001': {'result': 'rejected'}}, 'query': {}, 'receipts': []}}
        self.feed_data('rpa-run', data)
        c = self.claim()
        self.assertEqual((c['state'], c['bindings'], c['recoverable']), ('ready', [], 0))
        self.assertEqual(c['operations'][0]['state'], 'failed')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_shared_external_case_enters_dispute_without_merge(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_claim2.json')
        self.feed('portal-import', 'portal_dispute.json')
        ins = report(self.db)['insurance']
        self.assertEqual(ins['disputes'], [{'external_case': 'ext-1', 'claims': ['claim-1', 'claim-2']}])
        c1, c2 = self.claim('claim-1'), self.claim('claim-2')
        self.assertEqual((c1['state'], c1['recoverable']), ('disputed', 0))
        self.assertEqual(c1['bindings'][0]['state'], 'disputed')
        self.assertEqual(c2['state'], 'disputed')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_disputed_claim_blocks_new_intents(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('insurance-import', 'insurance_claim2.json')
        self.feed('portal-import', 'portal_dispute.json')
        with self.assertRaisesRegex(ValueError, 'disputed'):
            self.feed('rpa-run', 'rpa_append.json')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_duplicate_submit_while_case_live_blocked(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        with self.assertRaisesRegex(ValueError, 'append or withdraw first'):
            self.feed('rpa-run', 'rpa_refile.json')
        self.assertEqual(self.claim()['bindings'][0]['state'], 'active')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_operation_number_conflict_rejected(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        data = {'intents': [{'operation': 'op-0001', 'claim_id': 'claim-1', 'kind': 'submit', 'amount': 9999}],
                'portal': {'submit': {'op-0001': {'result': 'accepted', 'external_case': 'ext-9'}}, 'query': {}, 'receipts': []}}
        with self.assertRaisesRegex(ValueError, 'operation conflict'):
            self.feed_data('rpa-run', data)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_duplicate_scraped_receipt_deduped(self):
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('portal-import', 'portal_ok.json')
        data = json.loads(fixture('portal_ok.json'))
        data['receipts'][0]['event_id'] = 'portal-receipt-1-rescrape'
        self.feed_data('portal-import', data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM portal_receipt').fetchone()[0], 1)
        self.assertEqual(self.claim()['state'], 'submitted')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_receipt_before_notice_then_notice_catches_up(self):
        early = {'receipts': [{'event_id': 'early-1', 'claim_id': 'claim-9', 'external_case': 'ext-9',
                               'status': 'submitted', 'received_at': '2026-11-05T09:00:00'}]}
        self.feed_data('portal-import', early)
        c = self.claim('claim-9')
        self.assertEqual((c['state'], c['reason']), ('pending', 'notice missing'))
        notice = {'notices': [{'event_id': 'n-9', 'claim_id': 'claim-9', 'job': 'job-9',
                               'damage': 'cracked-sink', 'claimed_amount': 800, 'client_signer': 'client-9'}]}
        self.feed_data('insurance-import', notice)
        self.assertEqual(self.claim('claim-9')['state'], 'submitted')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_correction_receipt_before_original_receipt(self):
        # withdrawal receipt scraped before the original acceptance arrives
        self.feed('insurance-import', 'insurance_ok.json')
        self.feed('rpa-run', 'rpa_submit.json')
        self.feed('rpa-run', 'rpa_withdraw.json')
        self.feed('portal-import', 'portal_withdrawn.json')
        c = self.claim()
        self.assertEqual((c['state'], c['recoverable']), ('withdrawn', 0))
        self.assertEqual(self.chain(), [('ext-1', 'withdrawn')])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_cli_empty_db_to_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        seq = [('insurance-import', 'insurance_ok.json'), ('rpa-run', 'rpa_submit.json'),
               ('insurance-import', 'insurance_supplement.json'), ('rpa-run', 'rpa_withdraw.json'),
               ('portal-import', 'portal_withdrawn.json'), ('rpa-run', 'rpa_refile.json')]
        for command, name in seq:
            subprocess.run([sys.executable, '-B', '-m', 'homevisit', command, '--db', str(path), '--file', str(FIX / name)],
                           cwd=ROOT, text=True, capture_output=True, check=True)
        out = subprocess.run([sys.executable, '-B', '-m', 'homevisit', 'report', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        claim = json.loads(out.stdout)['insurance']['claims'][0]
        self.assertEqual([(c['external_case'], c['status']) for c in claim['case_chain']],
                         [('ext-1', 'withdrawn'), ('ext-2', 'accepted')])
        self.assertEqual((claim['recoverable'], claim['pending_review']), (6500, 0))
        out = subprocess.run([sys.executable, '-B', '-m', 'homevisit', 'audit', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
