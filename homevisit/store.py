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
CREATE TABLE IF NOT EXISTS offer_page(event_id TEXT PRIMARY KEY,vendor TEXT NOT NULL,service TEXT NOT NULL,region TEXT NOT NULL DEFAULT '',fetched_at TEXT NOT NULL,html TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_offer(event_id TEXT PRIMARY KEY,vendor TEXT NOT NULL,service TEXT NOT NULL,price INTEGER NOT NULL,unit TEXT NOT NULL,valid_to TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer_terms(event_id TEXT PRIMARY KEY,vendor TEXT NOT NULL,service TEXT NOT NULL,region TEXT NOT NULL,price INTEGER NOT NULL,unit TEXT NOT NULL,min_hours INTEGER NOT NULL,callout_fee INTEGER NOT NULL,callout_uncertain INTEGER NOT NULL,region_lock TEXT NOT NULL,promo_exclusive INTEGER NOT NULL,campaign TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,supersedes TEXT NOT NULL,footnotes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer_retraction(event_id TEXT PRIMARY KEY,vendor TEXT NOT NULL,service TEXT NOT NULL,region TEXT NOT NULL,retracts TEXT NOT NULL,fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_receipt(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,vendor TEXT NOT NULL,service TEXT NOT NULL,region TEXT NOT NULL,campaign TEXT NOT NULL,customer TEXT NOT NULL,hours INTEGER NOT NULL,unit_price INTEGER NOT NULL,callout_fee INTEGER NOT NULL,total INTEGER NOT NULL,settled_at TEXT NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offer_errata(event_id TEXT PRIMARY KEY,vendor TEXT NOT NULL,service TEXT NOT NULL,region TEXT NOT NULL,supersedes TEXT NOT NULL,waive_callout_fee INTEGER NOT NULL,note TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,signed_by TEXT NOT NULL,issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_quote(service TEXT NOT NULL,region TEXT NOT NULL DEFAULT '',state TEXT NOT NULL,price INTEGER NOT NULL,unit TEXT NOT NULL,amount INTEGER,source_event TEXT,proofs TEXT NOT NULL DEFAULT '[]',reason TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(service,region));
CREATE TABLE IF NOT EXISTS quote_version(id INTEGER PRIMARY KEY,service TEXT NOT NULL,region TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,price INTEGER NOT NULL,unit TEXT NOT NULL,amount INTEGER,source_event TEXT,proofs TEXT NOT NULL,reason TEXT NOT NULL,UNIQUE(service,region,version));
CREATE TABLE IF NOT EXISTS quote_delivery(event_id TEXT PRIMARY KEY,service TEXT NOT NULL,region TEXT NOT NULL,recipient TEXT NOT NULL,quote_version INTEGER NOT NULL,state TEXT NOT NULL,amount INTEGER,terms TEXT NOT NULL,proofs TEXT NOT NULL,at TEXT NOT NULL);
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
