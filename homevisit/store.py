import sqlite3

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS service_contract(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,version INTEGER NOT NULL,client TEXT NOT NULL,address TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,task TEXT NOT NULL,rate INTEGER NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_change(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,version INTEGER NOT NULL,kind TEXT NOT NULL,address TEXT,start TEXT,end TEXT,task TEXT,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,contract_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS worker_certificate(event_id TEXT PRIMARY KEY,worker TEXT NOT NULL,task TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cert_revocation(event_id TEXT PRIMARY KEY,worker TEXT NOT NULL,task TEXT NOT NULL,at TEXT NOT NULL,issuer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assignment(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,worker TEXT NOT NULL,at TEXT NOT NULL,client_approved TEXT NOT NULL,replace_of TEXT);
CREATE TABLE IF NOT EXISTS relay_request(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,at TEXT NOT NULL,from_worker TEXT NOT NULL,to_worker TEXT NOT NULL,client_approved TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handover_receipt(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,at TEXT NOT NULL,from_worker TEXT NOT NULL,to_worker TEXT NOT NULL,from_signed TEXT NOT NULL,to_signed TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responsibility_confirmation(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,route TEXT NOT NULL,client TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,worker TEXT,assignment_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS visit_receipt(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,address TEXT NOT NULL,worker TEXT NOT NULL,client TEXT,task TEXT);
CREATE TABLE IF NOT EXISTS task_acceptance(event_id TEXT PRIMARY KEY,job TEXT NOT NULL,task TEXT NOT NULL,accepted INTEGER NOT NULL,client TEXT NOT NULL,dispute TEXT,worker TEXT,win_start TEXT,win_end TEXT);
CREATE TABLE IF NOT EXISTS settlement_decision(job TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,minutes INTEGER NOT NULL,payable INTEGER NOT NULL,frozen INTEGER NOT NULL,version INTEGER NOT NULL,mode TEXT NOT NULL DEFAULT 'single',route TEXT,coverage_start TEXT,coverage_end TEXT,gap_minutes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS service_segment(job TEXT NOT NULL,route TEXT NOT NULL,idx INTEGER NOT NULL,start TEXT,end TEXT,worker TEXT,minutes INTEGER NOT NULL,payable INTEGER NOT NULL,frozen INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,relay_at TEXT,assignment_id TEXT,cert_event_id TEXT,handover_id TEXT,acceptance_id TEXT,dispute TEXT,PRIMARY KEY(job,route,idx));
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,job TEXT NOT NULL,version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

# Columns added after the first release; existing databases are upgraded in place.
_MIGRATIONS={
 'task_acceptance':[
   ("worker","ALTER TABLE task_acceptance ADD COLUMN worker TEXT"),
   ("win_start","ALTER TABLE task_acceptance ADD COLUMN win_start TEXT"),
   ("win_end","ALTER TABLE task_acceptance ADD COLUMN win_end TEXT"),
 ],
 'settlement_decision':[
   ("mode","ALTER TABLE settlement_decision ADD COLUMN mode TEXT NOT NULL DEFAULT 'single'"),
   ("route","ALTER TABLE settlement_decision ADD COLUMN route TEXT"),
   ("coverage_start","ALTER TABLE settlement_decision ADD COLUMN coverage_start TEXT"),
   ("coverage_end","ALTER TABLE settlement_decision ADD COLUMN coverage_end TEXT"),
   ("gap_minutes","ALTER TABLE settlement_decision ADD COLUMN gap_minutes INTEGER NOT NULL DEFAULT 0"),
 ],
}

def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    for table,columns in _MIGRATIONS.items():
        existing={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
        for name,ddl in columns:
            if name not in existing:db.execute(ddl)
    return db

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

def rebuild_segments(db,job,segments):
    """Replace every candidate-route segment decision for a job.

    Both candidate routes are rebuilt on every adjudication so that the route
    not confirmed by the client remains on record with its evidence.
    """
    db.execute('DELETE FROM service_segment WHERE job=?',(job,))
    for seg in segments:
        db.execute('INSERT INTO service_segment(job,route,idx,start,end,worker,minutes,payable,frozen,state,reason,relay_at,assignment_id,cert_event_id,handover_id,acceptance_id,dispute) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (job,seg['route'],seg['idx'],seg.get('start'),seg.get('end'),seg.get('worker'),seg['minutes'],seg['payable'],seg['frozen'],seg['state'],seg['reason'],seg.get('relay_at'),seg.get('assignment_id'),seg.get('cert_event_id'),seg.get('handover_id'),seg.get('acceptance_id'),seg.get('dispute')))
