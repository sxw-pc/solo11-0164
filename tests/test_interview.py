import copy
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


class InterviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def feed(self, data):
        return run_import(self.db, 'mail-import', json.dumps(data, ensure_ascii=False).encode())

    def feed_fixture(self, name):
        return self.feed(fixture(name))

    def seat(self, channel):
        for item in report(self.db)['interview_seats']:
            if item['seat'].endswith('|' + channel):
                return item
        self.fail(f'no {channel} seat')

    def cases(self):
        return {c['sender']: c['state'] for c in report(self.db)['applicant_cases']}

    def history(self, seat):
        rows = self.db.execute("SELECT state FROM decision_history WHERE subsystem='seat_decision' AND job=? ORDER BY id",
                               (seat,)).fetchall()
        return [r[0] for r in rows]

    def test_dual_seat_offers_ranked_by_declared_send_time(self):
        self.feed_fixture('interview_dual.json')
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'open')
        self.assertEqual([p['applicant'] for p in onsite['proposed']], ['jia@example.test'])
        self.assertEqual([w['applicant'] for w in onsite['waitlist']], ['yi@example.test'])
        remote = self.seat('remote')
        self.assertEqual([p['applicant'] for p in remote['proposed']], ['jia@example.test'])
        self.assertEqual(self.cases()['jia@example.test'], 'review')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_route1_confirm_onsite_keeps_waitlist(self):
        self.feed_fixture('interview_dual.json')
        self.feed_fixture('interview_route1.json')
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'committed')
        self.assertEqual([(c['applicant'], c['resource'], c['delivery']) for c in onsite['confirmed']],
                         [('jia@example.test', 'meeting-room', 'sent')])
        self.assertEqual([w['applicant'] for w in onsite['waitlist']], ['yi@example.test'])
        remote = self.seat('remote')
        self.assertEqual(remote['state'], 'closed')
        self.assertEqual(remote['confirmed'], [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_route2_confirm_remote_promotes_waitlist(self):
        self.feed_fixture('interview_dual.json')
        self.feed_fixture('interview_route2.json')
        remote = self.seat('remote')
        self.assertEqual([(c['applicant'], c['resource']) for c in remote['confirmed']],
                         [('jia@example.test', 'video-link')])
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'committed')
        self.assertEqual([c['applicant'] for c in onsite['confirmed']], ['yi@example.test'])
        self.assertEqual(onsite['waitlist'], [])
        self.assertEqual(onsite['manual'], [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_confirmation_bounce_rolls_back_only_affected_delivery(self):
        self.feed_fixture('interview_dual.json')
        self.feed_fixture('interview_route2.json')
        self.feed_fixture('interview_bounce.json')
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'manual_review')
        confirmed = {c['applicant']: c for c in onsite['confirmed']}
        self.assertEqual(confirmed['yi@example.test']['delivery'], 'failed')
        self.assertEqual(confirmed['yi@example.test']['status'], 'confirm_bounced')
        self.assertEqual({(n['kind'], n['applicant']) for n in onsite['notices']},
                         {('compensation', 'yi@example.test')})
        remote = self.seat('remote')
        self.assertEqual(remote['state'], 'partial')
        self.assertEqual(remote['manual'], [])
        self.assertEqual(remote['confirmed'][0]['delivery'], 'sent')
        self.assertEqual(self.history(onsite['seat']), ['open', 'committed', 'manual_review'])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_delayed_invite_bounce_does_not_erase_confirmed_seat(self):
        self.feed_fixture('interview_dual.json')
        self.feed_fixture('interview_route2.json')
        self.feed_fixture('interview_bounce.json')
        remote = self.seat('remote')
        self.assertEqual([c['applicant'] for c in remote['confirmed']], ['jia@example.test'])
        self.assertEqual(remote['confirmed'][0]['delivery'], 'sent')
        self.assertEqual(remote['manual'], [])

    def test_forwarded_quote_is_not_personal_confirmation(self):
        self.feed_fixture('interview_forward.json')
        onsite = self.seat('onsite')
        self.assertEqual([p['applicant'] for p in onsite['proposed']], ['bao@example.test'])
        senders = {m.get('sender') for m in onsite['manual'] if m['issue'] == 'untrusted-reply'}
        self.assertIn('wen@example.test', senders)
        self.assertIn('bao@example.test', senders)
        self.assertEqual(self.cases()['bao@example.test'], 'review')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_same_display_name_other_mailbox_not_merged(self):
        self.feed_fixture('interview_alias.json')
        remote = self.seat('remote')
        self.assertEqual(remote['proposed'], [])
        self.assertEqual([p['applicant'] for p in remote['pending']], ['alice@example.test'])
        self.assertEqual(remote['manual'][0]['issue'], 'untrusted-reply')
        cases = self.cases()
        self.assertEqual(cases['Alice Zhang <alice@example.test>'], 'review')
        self.assertEqual(cases['Alice Zhang <alice@example.net>'], 'logged')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_reply_promotes_ontime_waitlist_and_keeps_confirmation(self):
        data = fixture('interview_late.json')
        self.feed({'messages': data['messages'][:-1]})
        onsite = self.seat('onsite')
        self.assertEqual([p['applicant'] for p in onsite['proposed']], ['han@example.test'])
        self.assertEqual([(w['applicant'], w['late']) for w in onsite['waitlist']],
                         [('gao@example.test', True)])
        self.feed({'messages': data['messages'][-1:]})
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'manual_review')
        self.assertEqual([c['applicant'] for c in onsite['confirmed']], ['gao@example.test'])
        self.assertIn('confirmation-overrides-policy', {m['issue'] for m in onsite['manual']})
        self.assertEqual({n['applicant'] for n in onsite['notices'] if n['kind'] == 'compensation'},
                         {'han@example.test'})
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_import_order_does_not_change_allocation(self):
        data = fixture('interview_late.json')
        self.feed(data)
        first = report(self.db)['interview_seats']
        other_db = connect(Path(self.tmp.name) / 'other.sqlite')
        try:
            shuffled = copy.deepcopy(data)
            shuffled['messages'] = list(reversed(shuffled['messages']))
            run_import(other_db, 'mail-import', json.dumps(shuffled, ensure_ascii=False).encode())
            self.assertEqual(report(other_db)['interview_seats'], first)
        finally:
            other_db.close()

    def test_withdrawal_releases_unconfirmed_and_requires_receipt(self):
        data = fixture('interview_withdraw.json')
        self.feed({'messages': data['messages'][:6]})
        self.assertEqual(self.cases()['song@example.test'], 'withdrawn')
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'manual_review')
        self.assertEqual(onsite['confirmed'][0]['status'], 'cancelling')
        self.assertIn('cancel-pending-receipt', {m['issue'] for m in onsite['manual']})
        remote = self.seat('remote')
        self.assertEqual(remote['state'], 'closed')
        self.assertEqual(remote['confirmed'], [])
        self.feed({'messages': data['messages'][6:7]})
        self.assertEqual(self.seat('onsite')['confirmed'][0]['status'], 'cancelling')
        self.feed({'messages': data['messages'][7:]})
        onsite = self.seat('onsite')
        self.assertEqual(onsite['state'], 'closed')
        self.assertEqual(onsite['confirmed'], [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_public_cli_interview_seats(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        for name in ('interview_dual.json', 'interview_route2.json'):
            subprocess.run([sys.executable, '-m', 'homevisit', 'mail-import', '--db', str(path),
                            '--file', str(FIX / name)], cwd=ROOT, text=True, capture_output=True, check=True)
        out = subprocess.run([sys.executable, '-m', 'homevisit', 'report', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        seats = {s['seat'].rsplit('|', 1)[-1]: s for s in json.loads(out.stdout)['interview_seats']}
        self.assertEqual([c['applicant'] for c in seats['onsite']['confirmed']], ['yi@example.test'])
        self.assertEqual([c['applicant'] for c in seats['remote']['confirmed']], ['jia@example.test'])
        out = subprocess.run([sys.executable, '-m', 'homevisit', 'audit', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
