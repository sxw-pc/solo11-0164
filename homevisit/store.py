import sqlite3

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS service_contract(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,version INTEGER NOT NULL,client TEXT NOT NULL,address TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,task TEXT NOT NULL,rate INTEGER NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_change(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,version INTEGER NOT NULL,kind TEXT NOT NULL,address TEXT,start TEXT,end TEXT,task TEXT,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,contract_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS worker_certificate(event_id TEXT PRIMARY KEY,worker TEXT NOT NULL,task TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,worker TEXT NOT NULL,at TEXT NOT NULL,client_approved TEXT NOT NULL,replace_of TEXT);
CREATE TABLE IF NOT EXISTS dispatch_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,worker TEXT,assignment_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS visit_receipt(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,address TEXT NOT NULL,worker TEXT NOT NULL,client TEXT,task TEXT);
CREATE TABLE IF NOT EXISTS task_acceptance(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,task TEXT NOT NULL,accepted INTEGER NOT NULL,client TEXT NOT NULL,dispute TEXT);
CREATE TABLE IF NOT EXISTS settlement_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,minutes INTEGER NOT NULL,payable INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,job TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS answer_scan(event_id TEXT PRIMARY KEY,candidate TEXT NOT NULL,exam TEXT NOT NULL,scanned_at TEXT NOT NULL,answers TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exam_key(exam TEXT PRIMARY KEY,answers TEXT NOT NULL,pass_mark INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS exam_version(exam TEXT NOT NULL,version TEXT NOT NULL,question_map TEXT NOT NULL,pass_mark INTEGER NOT NULL,PRIMARY KEY(exam,version));
CREATE TABLE IF NOT EXISTS exam_version_key(exam TEXT NOT NULL,version TEXT NOT NULL,answers TEXT NOT NULL,PRIMARY KEY(exam,version));
CREATE TABLE IF NOT EXISTS knowledge_key(exam TEXT NOT NULL,question TEXT NOT NULL,answer TEXT NOT NULL,PRIMARY KEY(exam,question));
CREATE TABLE IF NOT EXISTS sheet_scan(event_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL,exam TEXT NOT NULL,candidate TEXT NOT NULL,scanned_at TEXT NOT NULL,answers TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sealed_bag(bag_id TEXT PRIMARY KEY,exam TEXT NOT NULL,version TEXT NOT NULL,sealed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bag_handoff(event_id TEXT NOT NULL,bag_id TEXT NOT NULL,paper_id TEXT,signer_a TEXT,signer_b TEXT,handed_at TEXT NOT NULL,PRIMARY KEY(event_id,bag_id));
CREATE TABLE IF NOT EXISTS sheet_issue(event_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,paper_id TEXT NOT NULL,room TEXT,seat_label TEXT NOT NULL,issued_at TEXT NOT NULL,signer_a TEXT NOT NULL,signer_b TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seat_chart(event_id TEXT NOT NULL,exam TEXT NOT NULL,room TEXT NOT NULL,seat_label TEXT NOT NULL,candidate TEXT NOT NULL,effective_at TEXT NOT NULL,PRIMARY KEY(event_id,seat_label));
CREATE TABLE IF NOT EXISTS seat_move(event_id TEXT PRIMARY KEY,exam TEXT NOT NULL,candidate TEXT NOT NULL,room TEXT NOT NULL,from_seat TEXT NOT NULL,to_seat TEXT NOT NULL,moved_at TEXT NOT NULL,signer_a TEXT,signer_b TEXT);
CREATE TABLE IF NOT EXISTS seat_move_correction(event_id TEXT PRIMARY KEY,exam TEXT NOT NULL,move_event_id TEXT NOT NULL,valid INTEGER NOT NULL,signer_a TEXT NOT NULL,signer_b TEXT NOT NULL,corrected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qr_fragment(event_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL,exam TEXT NOT NULL,version TEXT NOT NULL,bag_id TEXT,recovered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_conflict(paper_id TEXT NOT NULL,exam TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL,detected_at TEXT NOT NULL,PRIMARY KEY(paper_id,kind,detail));
CREATE TABLE IF NOT EXISTS paper_binding(paper_id TEXT PRIMARY KEY,exam TEXT NOT NULL,candidate TEXT NOT NULL,version TEXT,state TEXT NOT NULL,basis TEXT NOT NULL,reason TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exam_result(paper_id TEXT PRIMARY KEY,exam TEXT NOT NULL,candidate TEXT NOT NULL,version TEXT,state TEXT NOT NULL,score INTEGER NOT NULL,pass_mark INTEGER,source_event TEXT,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grade_notice(notice_id TEXT PRIMARY KEY,paper_id TEXT NOT NULL,exam TEXT NOT NULL,candidate TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,sent_at TEXT NOT NULL,replaces_notice TEXT);
"""

def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

def update(db,table,job,**fields):
    old=db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone()
    if old and all(old[k]==v for k,v in fields.items()):return dict(old)
    version=old['version']+1 if old else 1
    columns=['job',*fields,'version'];values=[job,*fields.values(),version]
    sql=f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(job) DO UPDATE SET "
    sql+=','.join(f'{c}=excluded.{c}' for c in [*fields,'version'])
    db.execute(sql,values)
    db.execute('INSERT INTO decision_history(subsystem,job,version,state,reason) VALUES(?,?,?,?,?)',(table,job,version,fields['state'],fields['reason']))
    return dict(db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone())
