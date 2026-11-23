import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from homevisit.__main__ import run_import, audit
from homevisit.source import offline_sign
from homevisit.store import connect
from homevisit.report import report

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def feed(db, data):
    return run_import(db, 'review-import', json.dumps(data, ensure_ascii=False).encode())


def declaration(event_id, kind, author, platform, key, pages):
    payload = {'type': 'declaration', 'kind': kind, 'author': author,
               'platform': platform, 'review_key': key, 'pages': pages}
    return {'event_id': event_id, 'kind': kind, 'author': author, 'platform': platform,
            'review_key': key, 'pages': pages, 'signature': offline_sign(payload)}


def page(event_id, platform, key, job, rating, published, author='client-a'):
    html = (f'<html><head><meta name="review-key" content="{key}"><meta name="job" content="{job}">'
            f'<meta name="rating" content="{rating}"><meta name="published" content="{published}">'
            f'<meta name="author" content="{author}"></head></html>')
    return {'event_id': event_id, 'platform': platform, 'review_key': key,
            'fetched_at': published, 'html': html}


class ReviewSourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def quality(self, job='job-1'):
        return dict(self.db.execute('SELECT * FROM quality_case WHERE job=?', (job,)).fetchone())

    def entry(self, platform, key, job='job-1'):
        row = self.db.execute('SELECT * FROM review_entry WHERE platform=? AND review_key=? AND job=?',
                              (platform, key, job)).fetchone()
        return dict(row) if row else None

    def attribution(self, event_id):
        return dict(self.db.execute('SELECT * FROM page_attribution WHERE event_id=?', (event_id,)).fetchone())

    def cases(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM identity_case ORDER BY case_id').fetchall()]

    def test_edit_recomputed_by_publish_version(self):
        feed(self.db, fixture('review_edit_1.json'))
        self.assertEqual(self.entry('site-a', 'r-2')['source_event'], 'page-e1')
        self.assertEqual(self.quality()['negative_count'], 1)
        feed(self.db, fixture('review_edit_2.json'))
        entry = self.entry('site-a', 'r-2')
        # page-e2 was fetched before page-e1 but published later: publish version wins
        self.assertEqual((entry['source_event'], entry['rating']), ('page-e2', 4))
        self.assertEqual(self.quality()['state'], 'clear')
        self.assertEqual(self.quality()['negative_count'], 0)
        self.assertEqual(self.attribution('page-e1')['role'], 'superseded')
        self.assertEqual(self.attribution('page-e2')['role'], 'effective')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_retract_only_referenced_versions(self):
        feed(self.db, fixture('review_retract.json'))
        self.assertEqual(self.entry('site-a', 'r-3')['retracted'], 1)
        self.assertEqual(self.attribution('page-x1')['role'], 'retracted')
        other = self.entry('site-b', 'r-8')
        self.assertEqual((other['retracted'], other['rating']), (0, 2))
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['state']), (1, 'review'))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_retract_latest_version_falls_back(self):
        feed(self.db, {'pages': [page('page-v1', 'site-a', 'r-9', 'job-1', 1, '2026-11-03T10:00:00'),
                                 page('page-v2', 'site-a', 'r-9', 'job-1', 5, '2026-11-03T12:00:00')]})
        self.assertEqual(self.entry('site-a', 'r-9')['rating'], 5)
        self.assertEqual(self.quality()['state'], 'clear')
        feed(self.db, {'declarations': [declaration('decl-v2', 'retract', 'client-a', 'site-a', 'r-9', ['page-v2'])]})
        # only v2 is withdrawn; the claim falls back to the earlier low version
        self.assertEqual(self.entry('site-a', 'r-9')['rating'], 1)
        self.assertEqual(self.quality()['negative_count'], 1)
        feed(self.db, {'declarations': [declaration('decl-v1', 'retract', 'client-a', 'site-a', 'r-9', ['page-v1'])]})
        self.assertEqual(self.entry('site-a', 'r-9')['retracted'], 1)
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['state']), (0, 'clear'))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_key_reuse_preserves_history(self):
        feed(self.db, fixture('review_key_reuse.json'))
        self.assertEqual(self.entry('site-a', 'r-1', 'job-1')['rating'], 1)
        self.assertEqual(self.entry('site-a', 'r-1', 'job-2')['rating'], 2)
        self.assertEqual(self.quality('job-1')['negative_count'], 1)
        self.assertEqual(self.quality('job-2')['negative_count'], 1)
        self.assertEqual(self.attribution('page-k1')['claim_id'], 'site-a:r-1:job-1')
        self.assertEqual(self.attribution('page-k2')['claim_id'], 'site-a:r-1:job-2')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_identical_content_without_signature_stays_separate(self):
        feed(self.db, fixture('review_similar.json'))
        self.assertEqual(self.quality()['negative_count'], 2)
        self.assertEqual(self.cases(), [])
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_signed_first_route_then_author_converges(self):
        feed(self.db, fixture('review_route_signed.json'))
        mirror = self.entry('site-b', 'r-7')
        self.assertEqual((mirror['merged_into'], mirror['merge_state']), ('site-a:r-1:job-1', 'tentative'))
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['pending_count']), (1, 1))
        self.assertIn('pending identity verification', quality['reason'])
        self.assertEqual(self.cases()[0]['state'], 'open')
        self.assertEqual(self.attribution('page-r7')['claim_id'], 'site-a:r-1:job-1')
        feed(self.db, fixture('review_author.json'))
        self.assertIsNone(self.entry('site-b', 'r-7')['merged_into'])
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['pending_count']), (2, 0))
        case = self.cases()[0]
        self.assertEqual((case['state'], case['resolution']), ('resolved', 'independent'))
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_conservative_route_then_author_converges(self):
        feed(self.db, fixture('review_route_conservative.json'))
        pending = self.entry('site-b', 'r-7')
        self.assertIsNone(pending['merged_into'])
        self.assertEqual(pending['pending'], 1)
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['pending_count']), (1, 1))
        self.assertEqual(self.attribution('page-r7')['claim_id'], 'site-b:r-7:job-1')
        feed(self.db, fixture('review_author.json'))
        quality = self.quality()
        self.assertEqual((quality['negative_count'], quality['pending_count']), (2, 0))
        self.assertEqual(self.cases()[0]['resolution'], 'independent')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_routes_converge_to_same_facts(self):
        with tempfile.TemporaryDirectory() as other:
            peer = connect(Path(other) / 'state.sqlite')
            try:
                feed(self.db, fixture('review_route_signed.json'))
                feed(peer, fixture('review_route_conservative.json'))
                # the two routes legitimately differ before authorship is proven
                self.assertNotEqual(report(self.db)['reviews'], report(peer)['reviews'])
                feed(self.db, fixture('review_author.json'))
                feed(peer, fixture('review_author.json'))
                mine, theirs = report(self.db), report(peer)
                for key in ('reviews', 'attributions', 'identity_cases', 'quality_cases'):
                    self.assertEqual(mine[key], theirs[key])
            finally:
                peer.close()

    def test_mirror_confirmation_converges_on_both_routes(self):
        for route in ('review_route_signed.json', 'review_route_conservative.json'):
            with tempfile.TemporaryDirectory() as tmp:
                db = connect(Path(tmp) / 'state.sqlite')
                try:
                    feed(db, fixture(route))
                    feed(db, {'declarations': [declaration('decl-m', 'authorship', 'client-a', 'site-b', 'r-7', ['page-r7'])]})
                    entry = dict(db.execute("SELECT * FROM review_entry WHERE platform='site-b'").fetchone())
                    self.assertEqual((entry['merged_into'], entry['merge_state']), ('site-a:r-1:job-1', 'confirmed'))
                    quality = dict(db.execute("SELECT * FROM quality_case WHERE job='job-1'").fetchone())
                    self.assertEqual((quality['negative_count'], quality['pending_count']), (1, 0))
                    case = dict(db.execute('SELECT * FROM identity_case').fetchone())
                    self.assertEqual((case['state'], case['resolution']), ('resolved', 'mirror'))
                    self.assertEqual(audit(db)['status'], 'matched')
                finally:
                    db.close()

    def test_invalid_repost_signature_rejected(self):
        data = fixture('review_route_conservative.json')
        data['reposts'][0]['signature'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'repost signature invalid'):
            feed(self.db, data)

    def test_invalid_declaration_signature_rejected(self):
        data = fixture('review_author.json')
        data['declarations'][0]['pages'] = ['page-other']
        with self.assertRaisesRegex(ValueError, 'declaration signature invalid'):
            feed(self.db, data)

    def test_review_cli(self):
        path = Path(self.tmp.name) / 'cli.sqlite'
        for name in ('review_route_conservative.json', 'review_author.json'):
            subprocess.run([sys.executable, '-m', 'homevisit', 'review-import', '--db', str(path),
                            '--file', str(FIX / name)], cwd=ROOT, capture_output=True, check=True)
        out = subprocess.run([sys.executable, '-m', 'homevisit', 'report', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        data = json.loads(out.stdout)
        self.assertEqual(data['quality_cases'][0]['negative_count'], 2)
        self.assertEqual(data['identity_cases'][0]['resolution'], 'independent')
        out = subprocess.run([sys.executable, '-m', 'homevisit', 'audit', '--db', str(path)],
                             cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
