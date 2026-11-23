import json


def report(db):
    jobs = []
    for row in db.execute('SELECT DISTINCT job FROM service_contract ORDER BY job'):
        job = row['job']
        item = {'job': job}
        for table, key in [('contract_decision', 'contract'), ('dispatch_decision', 'dispatch'),
                           ('settlement_decision', 'settlement')]:
            found = db.execute(f'SELECT * FROM {table} WHERE job=?', (job,)).fetchone()
            item[key] = dict(found) if found else None
        item['history'] = [dict(r) for r in db.execute(
            'SELECT subsystem,version,state,reason FROM decision_history WHERE job=? ORDER BY id', (job,))]
        jobs.append(item)

    papers = []
    for row in db.execute('SELECT * FROM paper_binding ORDER BY exam,paper_id'):
        paper = dict(row)
        paper['conflicts'] = [dict(r) for r in db.execute(
            'SELECT kind,detail,detected_at FROM paper_conflict WHERE paper_id=? ORDER BY kind,detail',
            (row['paper_id'],))]
        paper['notices'] = [dict(r) for r in db.execute(
            'SELECT notice_id,kind,sent_at,replaces_notice,body FROM grade_notice WHERE paper_id=? '
            'ORDER BY sent_at,notice_id', (row['paper_id'],))]
        papers.append(paper)

    return {
        'jobs': jobs,
        'papers': papers,
        'exam_results': [dict(r) for r in db.execute('SELECT * FROM exam_result ORDER BY exam,paper_id')],
        'grade_notices': [dict(r) for r in db.execute(
            'SELECT notice_id,paper_id,exam,candidate,kind,sent_at,replaces_notice FROM grade_notice '
            'ORDER BY sent_at,notice_id')],
        'evidence_count': db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
        'source_files': [dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')],
    }


def dumps(db):
    return json.dumps(report(db), ensure_ascii=False, indent=2)
