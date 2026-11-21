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
CREATE TABLE IF NOT EXISTS review_page(event_id TEXT PRIMARY KEY,platform TEXT NOT NULL,review_key TEXT NOT NULL,fetched_at TEXT NOT NULL,html TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_entry(platform TEXT NOT NULL,review_key TEXT NOT NULL,job TEXT NOT NULL,rating INTEGER NOT NULL,body TEXT NOT NULL,source_event TEXT NOT NULL,PRIMARY KEY(platform,review_key));
CREATE TABLE IF NOT EXISTS quality_case(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,negative_count INTEGER NOT NULL,version INTEGER NOT NULL);
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
