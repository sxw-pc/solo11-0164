from .source import record
from . import segment

def decide(db,job):
    # All settlement arithmetic lives in the segmented adjudication engine;
    # jobs without relay evidence take the single-worker path that mirrors the
    # original start/end + pause + acceptance rules.
    return segment.adjudicate(db,job)

def import_data(db,data):
    touched=set()
    with db:
        for row in data.get('receipts',[]):
            if record(db,row['event_id'],'receipt',row):
                db.execute('INSERT INTO visit_receipt VALUES(?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['kind'],row['at'],row['address'],row['worker'],row.get('client'),row.get('task')))
            touched.add(row['job'])
        for row in data.get('acceptances',[]):
            if record(db,row['event_id'],'acceptance',row):
                db.execute('INSERT INTO task_acceptance VALUES(?,?,?,?,?,?,?,?,?)',
                           (row['event_id'],row['job'],row['task'],int(row['accepted']),row['client'],row.get('dispute'),
                            row.get('worker'),row.get('win_start'),row.get('win_end')))
            touched.add(row['job'])
        for row in data.get('confirmations',[]):
            if record(db,row['event_id'],'confirmation',row):
                db.execute('INSERT INTO responsibility_confirmation VALUES(?,?,?,?,?)',
                           (row['event_id'],row['job'],row['route'],row['client'],row['at']))
            touched.add(row['job'])
        return [decide(db,job) for job in sorted(touched)]
