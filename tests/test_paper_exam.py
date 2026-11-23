import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from homevisit.__main__ import run_import, audit
from homevisit.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
PAPER = FIX / 'paper'
EXAM = 'cleaning-safety-ab'


def load(name):
    return json.loads((PAPER / name).read_text(encoding='utf8'))


def feed(db, data):
    return run_import(db, 'exam-import', json.dumps(data, ensure_ascii=False).encode())


def base(db):
    feed(db, load('exam_versions.json'))
    feed(db, load('scan_p.json'))


def result(db, paper_id='P'):
    return dict(db.execute('SELECT * FROM exam_result WHERE paper_id=?', (paper_id,)).fetchone())


def binding(db, paper_id='P'):
    return dict(db.execute('SELECT * FROM paper_binding WHERE paper_id=?', (paper_id,)).fetchone())


def conflicts(db, paper_id='P'):
    return [r['kind'] for r in db.execute(
        'SELECT kind FROM paper_conflict WHERE paper_id=? ORDER BY kind', (paper_id,))]


def notices(db, paper_id='P'):
    return [dict(r) for r in db.execute(
        'SELECT notice_id,kind,sent_at,replaces_notice,body FROM grade_notice WHERE paper_id=? '
        'ORDER BY sent_at,notice_id',
        (paper_id,))]


class PaperExamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ---- baseline: uncertain scan stays pending, no lucky version ----
    def test_01_scan_alone_is_unverified_and_never_picks_passing_version(self):
        base(self.db)
        r = result(self.db)
        self.assertEqual(r['state'], 'pending_review')
        self.assertIsNone(r['version'])
        self.assertEqual(binding(self.db)['basis'], 'paper version unverified')

    # ---- route A: QR fragment + two independent invigilator signatures ----
    def test_02_route_a_binds_version_a_and_passes(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        r = result(self.db)
        self.assertEqual((r['version'], r['state'], r['score'], r['pass_mark']), ('A', 'passed', 3, 2))
        self.assertEqual(conflicts(self.db), [])

    # ---- route B: per-paper issue + dual-signed seat move, fails ----
    def test_03_route_b_binds_version_b_and_fails_with_one_correct(self):
        base(self.db)
        feed(self.db, load('route_b.json'))
        r = result(self.db)
        self.assertEqual((r['version'], r['state'], r['score']), ('B', 'failed', 1))
        self.assertLess(r['score'], r['pass_mark'])

    # ---- same physical paper rescanned is one attempt ----
    def test_04_repeat_scan_same_answers_is_one_attempt(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        feed(self.db, load('rescan_p.json'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM sheet_scan WHERE paper_id=?', ('P',)).fetchone()[0], 2)
        self.assertEqual(result(self.db)['source_event'], 'scan-p-1')
        self.assertIn('repeat_scan', conflicts(self.db))
        self.assertEqual(result(self.db)['state'], 'passed')

    def test_05_rescan_with_different_answers_holds_grade(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        feed(self.db, {'sheet_scans': [{
            'event_id': 'scan-p-other', 'paper_id': 'P', 'exam': EXAM, 'candidate': '甲',
            'scanned_at': '2026-11-04T16:00:00', 'answers': {'q1': 'D', 'q2': 'D', 'q3': 'D'}}]})
        self.assertEqual(result(self.db)['state'], 'pending_review')
        self.assertIn('rescan_mismatch', conflicts(self.db))

    # ---- mutually exclusive proofs: conflict, never the later one ----
    def test_06_exclusive_proofs_conflict_not_latest_wins(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        feed(self.db, load('route_b.json'))
        self.assertEqual(result(self.db)['state'], 'pending_review')
        self.assertIn('exclusive_version_proofs', conflicts(self.db))
        self.assertIsNone(result(self.db)['version'])

    # ---- same-name card on a different paper cannot touch P ----
    def test_07_same_name_other_paper_does_not_change_p(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        feed(self.db, load('samename_yi.json'))
        self.assertEqual((result(self.db)['version'], result(self.db)['state']), ('A', 'passed'))
        peer = result(self.db, 'P-peer')
        self.assertEqual(peer['state'], 'pending_review')
        self.assertNotIn('exclusive_version_proofs', conflicts(self.db))

    # ---- one handoff pointing at two papers freezes both, others still publish ----
    def test_08_shared_bag_handoff_freezes_both_papers_only(self):
        base(self.db)
        feed(self.db, load('route_a.json'))
        feed(self.db, {'sheet_scans': [{
            'event_id': 'scan-other-1', 'paper_id': 'P9', 'exam': EXAM, 'candidate': '丙',
            'scanned_at': '2026-11-04T13:20:00', 'answers': {'q1': 'A', 'q2': 'B', 'q3': 'C'}}],
            'handoffs': [{'event_id': 'hand-a-3', 'bag_id': 'bag-A', 'paper_id': 'P9',
                          'signer_a': 'invigilator-li', 'signer_b': 'invigilator-wang',
                          'handed_at': '2026-11-04T09:10:00'}],
            'qr_fragments': [{'event_id': 'qr-p9', 'paper_id': 'P9', 'exam': EXAM, 'version': 'A',
                              'bag_id': 'bag-A', 'recovered_at': '2026-11-04T14:10:00'}]})
        self.assertEqual(result(self.db)['state'], 'pending_review')
        self.assertEqual(result(self.db, 'P9')['state'], 'pending_review')
        self.assertIn('shared_bag_handoff', conflicts(self.db))
        self.assertIn('shared_bag_handoff', conflicts(self.db, 'P9'))

    # ---- single signature on a bag handoff is not a valid chain ----
    def test_09_single_signer_bag_chain_rejected(self):
        base(self.db)
        data = load('route_a.json')
        data['handoffs'][1]['signer_b'] = None
        feed(self.db, data)
        self.assertEqual(result(self.db)['state'], 'pending_review')
        self.assertTrue(any(c[0] == 'qr_ignored' for c in self.db.execute(
            'SELECT kind FROM paper_conflict WHERE paper_id=?', ('P',))))

    # ---- seat move signed on one side only breaks the seating chain ----
    def test_10_unsigned_seat_move_blocks_route_b(self):
        base(self.db)
        data = load('route_b.json')
        data['seat_moves'][0]['signer_b'] = None
        feed(self.db, data)
        self.assertEqual(result(self.db)['state'], 'pending_review')
        self.assertIn('issue_unproven', conflicts(self.db))

    # ---- late signed correction re-grades; old notice text/time stay, correction issued ----
    def test_11_late_seat_correction_regrades_and_keeps_notice_history(self):
        base(self.db)
        data = load('route_b.json')
        # Insert an unsigned move earlier than the signed one: the chain first fails.
        data['seat_moves'].insert(0, {
            'event_id': 'move-jia-bad', 'exam': EXAM, 'candidate': '甲', 'room': 'room-1',
            'from_seat': 'S07', 'to_seat': 'S12', 'moved_at': '2026-11-04T08:55:00',
            'signer_a': None, 'signer_b': None})
        feed(self.db, data)
        self.assertEqual(result(self.db)['state'], 'pending_review')
        first_notices = notices(self.db)
        self.assertTrue(first_notices)
        first_body, first_sent = first_notices[0]['body'], first_notices[0]['sent_at']

        feed(self.db, load('seat_correction.json'))
        self.assertEqual((result(self.db)['version'], result(self.db)['state']), ('B', 'failed'))
        history = notices(self.db)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['kind'], 'result')
        self.assertEqual((history[0]['body'], history[0]['sent_at']), (first_body, first_sent))
        self.assertEqual(history[1]['kind'], 'correction')
        self.assertEqual(history[1]['replaces_notice'], history[0]['notice_id'])
        self.assertGreater(history[1]['sent_at'], first_sent)

    # ---- notices are immutable and externally issued notices survive re-grade ----
    def test_12_explicit_notice_never_rewritten(self):
        base(self.db)
        feed(self.db, load('samename_yi.json'))
        before = self.db.execute(
            "SELECT body,sent_at FROM grade_notice WHERE notice_id='notice-manual-peer'").fetchone()
        feed(self.db, load('route_a.json'))
        after = self.db.execute(
            "SELECT body,sent_at FROM grade_notice WHERE notice_id='notice-manual-peer'").fetchone()
        self.assertEqual(tuple(before), tuple(after))

    # ---- audit replay over the full A/B evidence lifecycle ----
    def test_13_replay_matches_after_evidence_and_corrections(self):
        base(self.db)
        feed(self.db, load('route_b.json'))
        feed(self.db, load('rescan_p.json'))
        feed(self.db, load('seat_correction.json'))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_13b_replay_matches_over_every_paper_fixture(self):
        for name in ('exam_versions', 'scan_p', 'route_a', 'route_b', 'rescan_p',
                     'samename_yi', 'seat_correction'):
            feed(self.db, load(f'{name}.json'))
        self.assertEqual(audit(self.db)['status'], 'matched')
        # Unrelated paper P-peer stays pending while conflicted P is held; nothing is published by name luck.
        states = {r['paper_id']: r['state'] for r in self.db.execute('SELECT paper_id,state FROM exam_result')}
        self.assertEqual(states['P-peer'], 'pending_review')
        self.assertEqual(states['P'], 'pending_review')

    def test_13c_legacy_repeat_scan_event_is_one_paper_one_attempt(self):
        data = json.loads((FIX / 'exam_ok.json').read_text(encoding='utf8'))
        feed(self.db, data)
        again = {'keys': data['keys'], 'scans': [{
            'event_id': 'scan-1', 'candidate': 'worker-applicant-1', 'exam': 'cleaning-safety',
            'scanned_at': '2026-11-04T13:00:00', 'answers': {'q1': 'A', 'q2': 'B'}}]}
        feed(self.db, again)
        rows = self.db.execute('SELECT COUNT(*) FROM exam_result').fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(audit(self.db)['status'], 'matched')

    # ---- CLI: scan -> evidence -> report -> audit, idempotent replay ----
    def test_14_cli_scan_to_report_and_audit(self):
        db_path = Path(self.tmp.name) / 'cli.sqlite'
        for name in ('exam_versions.json', 'scan_p.json', 'route_a.json', 'rescan_p.json'):
            proc = subprocess.run(
                [sys.executable, '-B', '-m', 'homevisit', 'exam-import', '--db', str(db_path),
                 '--file', str(PAPER / name)], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = subprocess.run([sys.executable, '-B', '-m', 'homevisit', 'report', '--db', str(db_path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        payload = json.loads(rep.stdout)
        paper = next(p for p in payload['papers'] if p['paper_id'] == 'P')
        self.assertEqual((paper['version'], paper['state']), ('A', 'passed'))
        self.assertTrue(any(n['kind'] == 'result' for n in paper['notices']))
        aud = subprocess.run([sys.executable, '-B', '-m', 'homevisit', 'audit', '--db', str(db_path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(aud.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
