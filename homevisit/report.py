import json
from .pricing import quote_detail

def report(db):
    jobs=[]
    for row in db.execute('SELECT DISTINCT job FROM service_contract ORDER BY job'):
        job=row['job'];item={'job':job}
        for table,key in [('contract_decision','contract'),('dispatch_decision','dispatch'),('settlement_decision','settlement')]:
            found=db.execute(f'SELECT * FROM {table} WHERE job=?',(job,)).fetchone()
            item[key]=dict(found) if found else None
        item['history']=[dict(r) for r in db.execute('SELECT subsystem,version,state,reason FROM decision_history WHERE job=? ORDER BY id',(job,))]
        jobs.append(item)
    quotes=[]
    for row in db.execute('SELECT service,region FROM market_quote ORDER BY service,region'):
        detail=quote_detail(db,row['service'],row['region'])
        quote=detail['quote']
        quote['delivered_to']=[{'recipient':d['recipient'],'quote_version':d['quote_version'],'amount':d['amount'],
                                'at':d['at'],'delivery':d['event_id']} for d in detail['deliveries']]
        quotes.append(quote)
    return {'jobs':jobs,'market_quotes':quotes,
            'quote_versions':[v for row in db.execute('SELECT service,region FROM market_quote ORDER BY service,region')
                              for v in quote_detail(db,row['service'],row['region'])['versions']],
            'evidence_count':db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],
            'source_files':[dict(r) for r in db.execute('SELECT kind,sha FROM file_journal ORDER BY id')]}

def dumps(db):return json.dumps(report(db),ensure_ascii=False,indent=2)
