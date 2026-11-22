import argparse
import json
from pathlib import Path
from . import contract,dispatch,visit
from .source import file_record
from .store import connect
from .report import report,dumps

def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    return {'contract-import':contract.import_data,'worker-import':dispatch.import_data,'visit-import':visit.import_data}[command](db,data)

def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('source replay does not match persisted decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()

def _load(db,root,command,name):
    run_import(db,command,(root/name).read_bytes())

def main(argv=None):
    p=argparse.ArgumentParser(prog='homevisit')
    p.add_argument('command',choices=['contract-import','worker-import','visit-import','report','audit','demo','demo-relay'])
    p.add_argument('--db',required=True);p.add_argument('--file');a=p.parse_args(argv)
    db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        root=Path(__file__).parent.parent/'fixtures'
        if a.command=='demo':
            for command,name in [('contract-import','contract_ok.json'),('worker-import','worker_ok.json'),('visit-import','visit_ok.json')]:
                _load(db,root,command,name)
            print(dumps(db));return
        if a.command=='demo-relay':
            for command,name in [('contract-import','seg_relay_contract.json'),
                                 ('worker-import','seg_relay_workers.json'),
                                 ('visit-import','seg_relay_visit_base.json'),
                                 ('visit-import','seg_relay_visit_lead.json')]:
                _load(db,root,command,name)
            print(dumps(db));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
