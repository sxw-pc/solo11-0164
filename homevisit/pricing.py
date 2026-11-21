"""Baseline market quote from collected public offers."""


def decide(db, service):
    rows = db.execute('SELECT * FROM service_offer WHERE service=? ORDER BY price,event_id', (service,)).fetchall()
    if rows:
        best = rows[0]
        state, price, unit, source = 'quoted', best['price'], best['unit'], best['event_id']
    else:
        state, price, unit, source = 'pending', 0, '', None
    db.execute('INSERT INTO market_quote VALUES(?,?,?,?,?) ON CONFLICT(service) DO UPDATE SET state=excluded.state,price=excluded.price,unit=excluded.unit,source_event=excluded.source_event',
               (service, state, price, unit, source))
    return dict(db.execute('SELECT * FROM market_quote WHERE service=?', (service,)).fetchone())
