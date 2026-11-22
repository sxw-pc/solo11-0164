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

class MaterialCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(homevisit.__file__).resolve().parent,ROOT/'homevisit')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def material(self,job='job-1'):
        return next(j for j in report(self.db)['jobs'] if j['job']==job)['material']
    def jobs(self):return {j['job']:j for j in report(self.db)['jobs']}
    def base_visit(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        feed(self.db,'visit-import',fixture('visit_ok.json'))
    def recall_base(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        feed(self.db,'material-import',fixture('material_recall_base.json'))
        feed(self.db,'material-import',fixture('material_recall_event.json'))

    def test_visit_receipts_before_material_records_ready(self):
        self.base_visit()
        feed(self.db,'material-import',fixture('material_ok.json'))
        m=self.material()
        self.assertEqual(m['state'],'ready')
        self.assertEqual([(c['batch'],c['quantity']) for c in m['consumed']],[('CLN-2026-11A',1)])
        self.assertEqual(self.jobs()['job-1']['settlement']['state'],'settled')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_no_material_events_keep_single_and_multi_worker_decisions(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        other=copy.deepcopy(fixture('contract_ok.json')['contracts'][0])
        other.update(event_id='contract-b',job='job-b',client='client-b',start='2026-11-03T09:00',end='2026-11-03T12:00',signed_by='client-b')
        feed(self.db,'contract-import',{'contracts':[other]})
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        feed(self.db,'worker-import',{'certificates':[{'event_id':'cert-b','worker':'worker-b','task':'clean','valid_from':'2026-01-01','valid_to':'2027-01-01','issuer':'agency'}],
                                      'assignments':[{'event_id':'assignment-b','job':'job-b','worker':'worker-b','at':'2026-11-01T11:00','client_approved':'client-b'}]})
        feed(self.db,'visit-import',fixture('visit_ok.json'))
        jobs=self.jobs()
        self.assertEqual((jobs['job-1']['contract']['state'],jobs['job-1']['dispatch']['state'],jobs['job-1']['settlement']['state']),('active','assigned','settled'))
        self.assertEqual((jobs['job-b']['contract']['state'],jobs['job-b']['dispatch']['state']),('active','assigned'))
        self.assertIsNone(jobs['job-1']['material']);self.assertIsNone(jobs['job-b']['material'])
        self.assertEqual(report(self.db)['materials'],{'batches':[],'recalls':[],'pending_review':[]})
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_one_checkout_split_across_two_jobs(self):
        feed(self.db,'material-import',fixture('material_split.json'))
        jobs=self.jobs();m1,m2=jobs['job-1']['material'],jobs['job-2']['material']
        self.assertEqual((m1['state'],m2['state']),('ready','ready'))
        self.assertEqual((m1['consumed'][0]['quantity'],m1['held'][0]['quantity']),(1,1))
        self.assertEqual(m2['consumed'][0]['quantity'],1)
        batch=report(self.db)['materials']['batches'][0]
        self.assertEqual((batch['checked_out'],batch['consumed'],batch['available']),(3,2,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_unopened_return_reallocates_to_another_job(self):
        feed(self.db,'material-import',fixture('material_return.json'))
        feed(self.db,'material-import',{'checkouts':[{'event_id':'checkout-rt2','batch_event':'batch-rt','worker':'worker-b','quantity':1,'at':'2026-11-02T13:00','issued_by':'storekeeper-a','received_by':'worker-b','splits':[{'job':'job-2','quantity':1}]}]})
        jobs=self.jobs()
        self.assertEqual((jobs['job-1']['material']['state'],jobs['job-2']['material']['state']),('ready','ready'))
        batch=report(self.db)['materials']['batches'][0]
        self.assertEqual((batch['available'],batch['returned_unopened'],batch['quarantined']),(0,1,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_opened_return_quarantined_not_reallocated(self):
        data=fixture('material_return.json');data['returns'][0]['opened']=True
        feed(self.db,'material-import',data)
        feed(self.db,'material-import',{'checkouts':[{'event_id':'checkout-rt2','batch_event':'batch-rt','worker':'worker-b','quantity':1,'at':'2026-11-02T13:00','issued_by':'storekeeper-a','received_by':'worker-b','splits':[{'job':'job-2','quantity':1}]}]})
        jobs=self.jobs()
        self.assertEqual(jobs['job-1']['material']['state'],'ready')
        self.assertEqual(jobs['job-2']['material']['state'],'shortage')
        batch=report(self.db)['materials']['batches'][0]
        self.assertEqual((batch['available'],batch['quarantined']),(0,1))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_supply_shortage_status(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'material-import',fixture('material_shortage.json'))
        self.assertEqual(self.material()['state'],'shortage')
    def test_expired_batch_status(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'material-import',fixture('material_expired.json'))
        self.assertEqual(self.material()['state'],'expired')
    def test_unsigned_handover_status(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'material-import',fixture('material_unsigned.json'))
        self.assertEqual(self.material()['state'],'unsigned')
    def test_usage_must_point_to_valid_checkout(self):
        feed(self.db,'material-import',{'usages':[{'event_id':'usage-x','job':'job-1','checkout':'checkout-ghost','quantity':1,'at':'2026-11-02T09:30'}]})
        self.assertEqual(self.material()['state'],'review')
    def test_late_recall_after_acceptance_flags_consuming_service(self):
        self.base_visit()
        feed(self.db,'material-import',fixture('material_ok.json'))
        self.assertEqual(self.material()['state'],'ready')
        feed(self.db,'material-import',fixture('material_recall_late.json'))
        m=self.material()
        self.assertEqual(m['state'],'recall_risk')
        self.assertEqual(self.jobs()['job-1']['settlement']['state'],'settled')
        materials=report(self.db)['materials']
        self.assertEqual(materials['recalls'][0]['consumed_by'],['job-1'])
        self.assertEqual(materials['pending_review'],['job-1'])
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_recalled_batch_held_unopened_blocks_unsealing(self):
        self.recall_base()
        m=self.material()
        self.assertEqual(m['state'],'recall_hold')
        recall=report(self.db)['materials']['recalls'][0]
        self.assertEqual((recall['held_by'],recall['consumed_by']),(['job-1'],[]))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_substitute_route_keeps_dispatch_and_attributes_consumption(self):
        self.recall_base()
        self.assertEqual(self.material()['state'],'recall_hold')
        feed(self.db,'material-import',fixture('material_route_substitute.json'))
        m=self.material()
        self.assertEqual(m['state'],'ready')
        self.assertIn('substitut',m['reason'])
        self.assertEqual([(c['batch'],c['quantity']) for c in m['consumed']],[('CLN-888',1)])
        self.assertEqual(self.jobs()['job-1']['dispatch']['state'],'assigned')
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_reschedule_route_defers_and_keeps_equivalent_stock_unused(self):
        self.recall_base()
        feed(self.db,'material-import',fixture('material_route_reschedule.json'))
        m=self.material()
        self.assertEqual(m['state'],'deferred')
        self.assertEqual(m['consumed'],[])
        batch=next(b for b in report(self.db)['materials']['batches'] if b['event_id']=='batch-b')
        self.assertEqual((batch['available'],batch['consumed']),(3,0))
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_two_routes_diverge_on_same_facts(self):
        other=connect(Path(self.tmp.name)/'other.sqlite')
        try:
            for db in (self.db,other):
                feed(db,'contract-import',fixture('contract_ok.json'))
                feed(db,'worker-import',fixture('worker_ok.json'))
                feed(db,'material-import',fixture('material_recall_base.json'))
                feed(db,'material-import',fixture('material_recall_event.json'))
            feed(self.db,'material-import',fixture('material_route_substitute.json'))
            feed(other,'material-import',fixture('material_route_reschedule.json'))
            sub=next(j for j in report(self.db)['jobs'] if j['job']=='job-1')['material']
            res=next(j for j in report(other)['jobs'] if j['job']=='job-1')['material']
            self.assertEqual((sub['state'],res['state']),('ready','deferred'))
            self.assertEqual([c['batch'] for c in sub['consumed']],['CLN-888'])
            self.assertEqual(res['consumed'],[])
        finally:other.close()
    def test_late_usage_traces_only_the_other_service(self):
        self.recall_base()
        feed(self.db,'material-import',fixture('material_route_reschedule.json'))
        before=self.material()
        feed(self.db,'material-import',fixture('material_late_usage.json'))
        self.assertEqual(self.material(),before)
        job9=self.material('job-9')
        self.assertEqual(job9['state'],'recall_risk')
        self.assertEqual([c['batch'] for c in job9['consumed']],['CLN-777'])
        materials=report(self.db)['materials']
        self.assertEqual(materials['recalls'][0]['consumed_by'],['job-9'])
        self.assertEqual(materials['pending_review'],['job-9'])
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_same_batch_name_different_supplier_not_conflated(self):
        feed(self.db,'contract-import',fixture('contract_ok.json'))
        feed(self.db,'worker-import',fixture('worker_ok.json'))
        feed(self.db,'material-import',{
            'batches':[{'event_id':'batch-x','batch':'CLN-1','material':'surface-cleaner','task':'clean','supplier':'sup-x','source_doc':'po-1','quantity':1,'valid_from':'2026-10-01','valid_to':'2027-04-01'},
                       {'event_id':'batch-y','batch':'CLN-1','material':'surface-cleaner','task':'clean','supplier':'sup-y','source_doc':'po-2','quantity':1,'valid_from':'2026-10-01','valid_to':'2027-04-01'}],
            'plans':[{'event_id':'plan-n','job':'job-1','material':'surface-cleaner','task':'clean','quantity':1,'batch':'CLN-1','supplier':'sup-y','source_doc':'po-2'}],
            'checkouts':[{'event_id':'checkout-n','batch_event':'batch-y','worker':'worker-a','quantity':1,'at':'2026-11-02T08:00','issued_by':'storekeeper-a','received_by':'worker-a','splits':[{'job':'job-1','quantity':1}]}]})
        feed(self.db,'material-import',{'recalls':[{'event_id':'recall-x','batch_event':'batch-x','supplier':'sup-x','at':'2026-11-02T08:30','reason':'supplier recall'}]})
        m=self.material()
        self.assertEqual(m['state'],'ready')
        self.assertEqual(m['held'][0]['supplier'],'sup-y')
        dup={'batches':[{'event_id':'batch-x2','batch':'CLN-1','material':'surface-cleaner','task':'clean','supplier':'sup-x','source_doc':'po-1','quantity':1,'valid_from':'2026-10-01','valid_to':'2027-04-01'}]}
        with self.assertRaisesRegex(ValueError,'batch identity conflict'):feed(self.db,'material-import',dup)
        self.assertEqual(audit(self.db)['status'],'matched')
    def test_material_import_replay_idempotent(self):
        self.base_visit()
        data=fixture('material_ok.json')
        feed(self.db,'material-import',data)
        first=report(self.db)
        feed(self.db,'material-import',data)
        self.assertEqual(report(self.db),first)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM material_batch').fetchone()[0],1)
    def test_public_cli_material_flow(self):
        path=Path(self.tmp.name)/'cli.sqlite'
        out=subprocess.run([sys.executable,'-m','homevisit','demo','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        item=json.loads(out.stdout)['jobs'][0]
        self.assertEqual(item['material']['state'],'ready')
        self.assertEqual(item['settlement']['state'],'settled')
        out=subprocess.run([sys.executable,'-m','homevisit','audit','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(out.stdout)['status'],'matched')
    def test_public_cli_material_import_and_report(self):
        path=Path(self.tmp.name)/'cli2.sqlite'
        for command,name in [('contract-import','contract_ok.json'),('worker-import','worker_ok.json'),
                             ('material-import','material_recall_base.json'),('material-import','material_recall_event.json')]:
            subprocess.run([sys.executable,'-m','homevisit',command,'--db',str(path),'--file',str(FIX/name)],cwd=ROOT,text=True,capture_output=True,check=True)
        out=subprocess.run([sys.executable,'-m','homevisit','report','--db',str(path)],cwd=ROOT,text=True,capture_output=True,check=True)
        data=json.loads(out.stdout)
        self.assertEqual(data['jobs'][0]['material']['state'],'recall_hold')
        self.assertEqual(data['materials']['pending_review'],['job-1'])

if __name__=='__main__':unittest.main()
