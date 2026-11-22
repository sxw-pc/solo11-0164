import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import homevisit
from homevisit import export
from homevisit.__main__ import run_import, audit
from homevisit.store import connect
from homevisit.report import report

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf8'))


def cell(package, record, field):
    for item in package['records']:
        if item['record'] == record:
            return item['fields'].get(field)
    raise KeyError(record)


class ExportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')
        self.assertEqual(Path(homevisit.__file__).resolve().parent, ROOT / 'homevisit')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def feed(self, kind, name):
        raw = json.dumps(fixture(name), ensure_ascii=False).encode()
        return run_import(self.db, kind, raw)

    def mixed(self):
        self.feed('contract-import', 'contract_mixed.json')
        self.feed('worker-import', 'worker_mixed.json')
        self.feed('visit-import', 'visit_mixed.json')
        return self.feed('privacy-import', 'privacy_mixed.json')

    def build(self, *request_ids):
        raw = json.dumps({'requests': list(request_ids)}).encode()
        return run_import(self.db, 'export-build', raw)

    def body(self, request_id, version=None):
        if version:
            row = self.db.execute('SELECT body FROM export_delivery WHERE request_id=? AND version=?',
                                  (request_id, version)).fetchone()
        else:
            row = self.db.execute('SELECT body FROM export_delivery WHERE request_id=? ORDER BY version DESC',
                                  (request_id,)).fetchone()
        return row['body']

    def package(self, request_id, version=None):
        return json.loads(self.body(request_id, version))

    def package_state(self, request_id):
        return self.db.execute('SELECT state,reason FROM export_package WHERE request_id=?',
                               (request_id,)).fetchone()

    def test_dual_job_self_package(self):
        result = self.mixed()
        states = {row['request_id']: row['state'] for row in result}
        self.assertEqual(states, {'privacy-request-agent': 'ready', 'privacy-request-self': 'ready'})
        pkg = self.package('privacy-request-self')
        # 本人可见本人签署的服务条款(含合同地址)、到离场时间与验收结果
        self.assertEqual(cell(pkg, 'contract:job-1', 'address'),
                         {'status': 'disclosed', 'value': 'home-a',
                          'source': {'event_id': 'contract-1', 'pointer': '/address'}})
        self.assertEqual(cell(pkg, 'receipt:start-1', 'at')['value'], '2026-11-02T09:00')
        self.assertEqual(cell(pkg, 'receipt:end-1', 'at')['value'], '2026-11-02T11:00')
        self.assertEqual(cell(pkg, 'acceptance:accept-1', 'accepted')['value'], False)
        self.assertEqual(cell(pkg, 'acceptance:accept-1', 'dispute')['status'], 'disclosed')
        # 人员证书号遮盖
        cert = cell(pkg, 'certificate:cert-1', 'issuer')
        self.assertEqual(cert['status'], 'redacted')
        self.assertNotIn('value', cert)
        # 逐项遮盖理由与交付清单存在
        self.assertTrue(pkg['redactions'])
        self.assertEqual(pkg['manifest']['records'], len(pkg['records']))
        # 同一 visit-import 原文件中的 job-2 不得泄漏进 job-1 的包
        body = self.body('privacy-request-self')
        for leak in ('job-2', 'client-b', 'home-b', 'worker-b',
                     'cert-2', 'start-2', 'end-2', 'accept-2', 'assignment-2', 'contract-2'):
            self.assertNotIn(leak, body)
        # 来源证明只含 job-1 事件锚点,可离线核验
        self.assertTrue(pkg['created_from']['evidence'])
        self.assertEqual(export.verify(self.db, pkg)['status'], 'verified')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_agent_package_scope(self):
        self.mixed()
        pkg = self.package('privacy-request-agent')
        # 代办可见 job-1 争议与现场时间
        self.assertEqual(cell(pkg, 'receipt:start-1', 'at')['status'], 'disclosed')
        self.assertEqual(cell(pkg, 'acceptance:accept-1', 'dispute')['value'],
                         'door frame scratched during cleaning')
        # 门牌号遮盖
        for record in ('contract:job-1', 'receipt:start-1', 'receipt:end-1'):
            masked = cell(pkg, record, 'address')
            self.assertEqual(masked['status'], 'redacted')
            self.assertNotIn('value', masked)
        # 合同条款、验收结果、人员与资质、决定细节均不在委托 scope 内
        for record, field in [('contract:job-1', 'task'), ('contract:job-1', 'rate'),
                              ('acceptance:accept-1', 'accepted'), ('receipt:start-1', 'worker'),
                              ('certificate:cert-1', 'issuer'),
                              ('decision:settlement_decision:job-1', 'state')]:
            self.assertEqual(cell(pkg, record, field)['status'], 'redacted')
        body = self.body('privacy-request-agent')
        for leak in ('home-a', 'worker-a', 'job-2', 'client-b', 'home-b', 'worker-b'):
            self.assertNotIn(leak, body)
        # 同一原始文件与当前决定,两份包的权限范围与可交付字段不同
        self_pkg = self.package('privacy-request-self')
        self.assertNotEqual(self.body('privacy-request-agent'), self.body('privacy-request-self'))
        self.assertEqual(cell(self_pkg, 'receipt:start-1', 'address')['status'], 'disclosed')
        self.assertEqual(export.verify(self.db, pkg)['status'], 'verified')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_rebuild_same_version_idempotent(self):
        self.mixed()
        before = self.body('privacy-request-self')
        result = self.build('privacy-request-self')
        self.assertEqual(result[0]['state'], 'ready')
        self.assertIn('unchanged', result[0]['reason'])
        self.assertEqual(self.body('privacy-request-self'), before)
        count = self.db.execute("SELECT COUNT(*) FROM export_delivery WHERE request_id='privacy-request-self'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_late_reschedule_reissue_and_delegation_expiry(self):
        self.mixed()
        self_v1 = self.body('privacy-request-self')
        agent_v1 = self.body('privacy-request-agent')
        # 迟到的合同改期只影响之后制作的包
        self.feed('contract-import', 'contract_late.json')
        self.build()
        rows = self.db.execute('SELECT version,superseded FROM export_delivery WHERE request_id=? '
                               'ORDER BY version', ('privacy-request-self',)).fetchall()
        self.assertEqual([(row['version'], row['superseded']) for row in rows], [(1, 1), (2, 0)])
        pkg2 = self.package('privacy-request-self')
        self.assertEqual(cell(pkg2, 'contract:job-1', 'start')['value'], '2026-11-03T09:00')
        self.assertEqual(cell(pkg2, 'contract:job-1', 'start')['source'],
                         {'event_id': 'change-1', 'pointer': '/start'})
        # 已交付包的字节与交付事实保留,旧版本仍可离线核验
        self.assertEqual(self.body('privacy-request-self', 1), self_v1)
        result = export.verify(self.db, json.loads(self_v1))
        self.assertEqual((result['status'], result['superseded']), ('verified', True))
        # 报告标明需向客户补发哪一版本
        reissue = {row['request_id']: row['version'] for row in report(self.db)['reissue_required']}
        self.assertEqual(reissue, {'privacy-request-self': 2})
        # 委托到期只阻止新制作,已签收的包保留
        agent = self.package_state('privacy-request-agent')
        self.assertEqual(agent['state'], 'blocked')
        self.assertIn('expired', agent['reason'])
        count = self.db.execute("SELECT COUNT(*) FROM export_delivery WHERE request_id='privacy-request-agent'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.body('privacy-request-agent', 1), agent_v1)
        self.assertEqual(export.verify(self.db, json.loads(agent_v1))['status'], 'verified')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_unsplittable_source_blocked(self):
        self.mixed()
        self.feed('visit-import', 'visit_unsplittable.json')
        self.build('privacy-request-self')
        pkg = self.package('privacy-request-self')
        # 混装字段被阻断而不是为维持 hash 校验泄露整份原文
        blocked = cell(pkg, 'acceptance:accept-9', 'dispute')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertNotIn('value', blocked)
        # 待补的来源定位列入 pending
        pending = [item for item in pkg['pending'] if item['record'] == 'acceptance:accept-9']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['source'], {'event_id': 'accept-9', 'pointer': '/dispute'})
        # pending 只给来源定位,不夹带混装原文
        self.assertNotIn('witness', json.dumps(pending[0], ensure_ascii=False))
        # 同一事件的其余干净字段仍正常披露
        self.assertEqual(cell(pkg, 'acceptance:accept-9', 'accepted'),
                         {'status': 'disclosed', 'value': False,
                          'source': {'event_id': 'accept-9', 'pointer': '/accepted'}})
        body = self.body('privacy-request-self')
        for leak in ('client-b', 'home-b', 'job-2'):
            self.assertNotIn(leak, body)
        self.assertEqual(export.verify(self.db, pkg)['status'], 'verified')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_withdrawal_blocks_new_build_keeps_delivered(self):
        self.mixed()
        agent_v1 = self.body('privacy-request-agent')
        self.feed('privacy-import', 'privacy_withdraw.json')
        agent = self.package_state('privacy-request-agent')
        self.assertEqual(agent['state'], 'blocked')
        self.assertIn('withdrawn', agent['reason'])
        # 已交付的代办包与本人包均不受撤回影响
        self.assertEqual(self.body('privacy-request-agent', 1), agent_v1)
        self.assertEqual(export.verify(self.db, json.loads(agent_v1))['status'], 'verified')
        result = self.build('privacy-request-self')
        self.assertEqual(result[0]['state'], 'ready')
        self.assertEqual(audit(self.db)['status'], 'matched')

    def test_verify_rejects_tampered_package(self):
        self.mixed()
        pkg = self.package('privacy-request-self')
        pkg['records'][0]['fields']['address']['value'] = 'home-z'
        with self.assertRaises(ValueError):
            export.verify(self.db, pkg)
        pkg = self.package('privacy-request-self')
        pkg['records'][0]['fields']['address'] = {'status': 'redacted', 'reason': 'hidden'}
        with self.assertRaises(ValueError):
            export.verify(self.db, pkg)

    def test_cli_build_verify_audit(self):
        path = Path(self.tmp.name) / 'cli.sqlite'

        def cli(*args):
            return subprocess.run([sys.executable, '-B', '-m', 'homevisit', *args, '--db', str(path)],
                                  cwd=ROOT, text=True, capture_output=True, check=True)

        for command, name in [('contract-import', 'contract_mixed.json'), ('worker-import', 'worker_mixed.json'),
                              ('visit-import', 'visit_mixed.json'), ('privacy-import', 'privacy_mixed.json')]:
            cli(command, '--file', str(FIX / name))
        out = Path(self.tmp.name) / 'pkg-self.json'
        built = json.loads(cli('export-build', '--request', 'privacy-request-self', '--out', str(out)).stdout)
        self.assertEqual(built[0]['state'], 'ready')
        verified = json.loads(cli('export-verify', '--package', str(out)).stdout)
        self.assertEqual(verified['status'], 'verified')
        self.assertEqual(json.loads(cli('audit').stdout)['status'], 'matched')


if __name__ == '__main__':
    unittest.main()
