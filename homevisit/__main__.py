import argparse
import json
from pathlib import Path
from . import contract,dispatch,visit,privacy,export
from .source import file_record
from .store import connect
from .report import report,dumps

def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    handlers={'contract-import':contract.import_data,'worker-import':dispatch.import_data,
              'visit-import':visit.import_data,'privacy-import':privacy.import_data,
              'export-build':lambda db,data:export.build_requests(db,data.get('requests'))}
    return handlers[command](db,data)

def audit(db):
    replay=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(replay,row['kind'],row['raw'])
        if report(db)!=report(replay):raise ValueError('source replay does not match persisted decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:replay.close()

def main(argv=None):
    p=argparse.ArgumentParser(prog='homevisit')
    p.add_argument('command',choices=['contract-import','worker-import','visit-import','privacy-import',
                                      'export-build','export-verify','report','audit','demo'])
    p.add_argument('--db',required=True);p.add_argument('--file');p.add_argument('--request')
    p.add_argument('--out');p.add_argument('--package');a=p.parse_args(argv)
    db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('contract-import','contract_ok.json'),('worker-import','worker_ok.json'),('visit-import','visit_ok.json')]:
                run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if a.command=='export-verify':
            if not a.package:p.error('--package required')
            package=json.loads(Path(a.package).read_text(encoding='utf8'))
            print(json.dumps(export.verify(db,package),ensure_ascii=False,indent=2));return
        if a.command=='export-build':
            raw=json.dumps({'requests':[a.request] if a.request else []},ensure_ascii=False).encode()
            result=run_import(db,a.command,raw)
            if a.out:
                if not a.request:p.error('--out requires --request')
                row=db.execute('SELECT body FROM export_delivery WHERE request_id=? ORDER BY version DESC',(a.request,)).fetchone()
                if not row:p.error('no delivered package for request')
                Path(a.out).write_text(row['body'],encoding='utf8')
            print(json.dumps(result,ensure_ascii=False,indent=2));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
