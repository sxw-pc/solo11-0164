import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import homevisit
from homevisit.__main__ import run_import,audit
from homevisit.store import connect
from homevisit.report import report

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class VisitCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(homevisit.__file__).resolve().parent,ROOT/'homevisit')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def state(self,table):return dict(self.db.execute(f'SELECT * FROM {table} WHERE job=?',('job-1',)).fetchone())
    def normal(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        feed(self.db,'visit-import',fixture('visit_ok.json'))
        return report(self.db)['jobs'][0]
    def test_normal_contract_dispatch_visit(self):
        item=self.normal()
        self.assertEqual((item['contract']['state'],item['dispatch']['state'],item['settlement']['state']),('active','assigned','settled'))
        self.assertEqual((item['settlement']['minutes'],item['settlement']['payable']),(90,9000))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_contract_reschedule_retracts_old_visit(self):
        self.normal();feed(self.db,'contract-import',fixture('contract_bad.json'))
        self.assertNotEqual(self.state('settlement_decision')['state'],'settled')
        self.assertGreater(self.state('settlement_decision')['version'],3)
    def test_unsigned_addon_does_not_change_task(self):
        self.normal();data=fixture('contract_bad.json');data['changes'][0].update(kind='addon',task='deep-clean',signed_by='agency')
        feed(self.db,'contract-import',data)
        self.assertEqual(self.state('contract_decision')['state'],'active')
        self.assertEqual(self.state('settlement_decision')['state'],'settled')
    def test_client_addon_requires_new_worker_qualification(self):
        self.normal();data=fixture('contract_bad.json');data['changes'][0].update(kind='addon',task='deep-clean',address=None,start=None,end=None)
        feed(self.db,'contract-import',data)
        self.assertEqual(self.state('dispatch_decision')['state'],'blocked')
        self.assertEqual(self.state('settlement_decision')['state'],'blocked')
    def test_parallel_contract_conflict_freezes(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        data=fixture('contract_ok.json');data['contracts'][0]['event_id']='contract-2';data['contracts'][0]['address']='home-b'
        feed(self.db,'contract-import',data)
        self.assertEqual(self.state('contract_decision')['state'],'frozen')
    def test_expired_worker_certificate_blocks_dispatch(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        data=fixture('worker_ok.json');data['certificates'][0]['valid_to']='2026-11-02T10:00'
        feed(self.db,'worker-import',data)
        self.assertEqual(self.state('dispatch_decision')['state'],'blocked')
    def test_unauthorized_replacement_blocks_hours(self):
        self.normal();feed(self.db,'worker-import',fixture('worker_bad.json'))
        self.assertEqual(self.state('dispatch_decision')['state'],'blocked')
        self.assertEqual(self.state('settlement_decision')['state'],'blocked')
    def test_overlap_assignment_waitlisted(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        data=fixture('contract_ok.json');other=copy.deepcopy(data['contracts'][0]);other.update(event_id='contract-b',job='job-b',start='2026-11-02T10:00',end='2026-11-02T13:00')
        feed(self.db,'contract-import',{'contracts':[other]})
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        peer={'assignments':[{'event_id':'assignment-b','job':'job-b','worker':'worker-a','at':'2026-11-01T11:00','client_approved':'client-a'}]}
        feed(self.db,'worker-import',peer)
        self.assertEqual(self.db.execute("SELECT state FROM dispatch_decision WHERE job='job-b'").fetchone()[0],'waitlist')
    def test_single_sided_visit_receipt_disputed(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'));feed(self.db,'worker-import',fixture('worker_ok.json'))
        data=fixture('visit_ok.json');data['receipts'][1]['client']=None
        data['receipts'][0]['client']=None
        feed(self.db,'visit-import',data)
        self.assertEqual(self.state('settlement_decision')['state'],'disputed')
    def test_pause_deducts_minutes(self):
        item=self.normal();self.assertEqual(item['settlement']['minutes'],90)
    def test_unmatched_pause_disputes(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'));feed(self.db,'worker-import',fixture('worker_ok.json'))
        data=fixture('visit_ok.json');data['receipts']=[r for r in data['receipts'] if r['kind']!='resume']
        feed(self.db,'visit-import',data)
        self.assertEqual(self.state('settlement_decision')['state'],'disputed')
    def test_task_dispute_freezes_settlement(self):
        self.normal();feed(self.db,'visit-import',fixture('visit_bad.json'))
        self.assertEqual(self.state('settlement_decision')['state'],'disputed')
        self.assertEqual((self.state('settlement_decision')['payable'],self.state('settlement_decision')['frozen']),(0,9000))
    def test_event_replay_and_conflict(self):
        data=fixture('contract_ok.json');feed(self.db,'contract-import',data);feed(self.db,'contract-import',data)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM service_contract').fetchone()[0],1)
        data['contracts'][0]['address']='other'
        with self.assertRaisesRegex(ValueError,'event_id conflict'):feed(self.db,'contract-import',data)
    def test_public_cli(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        out=subprocess.run([sys.executable,'-m','homevisit','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['jobs'][0]['settlement']['state'],'settled')

if __name__=='__main__':unittest.main()
