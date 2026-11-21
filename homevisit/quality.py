"""Baseline quality case derived from collected service reviews."""

from .store import update


def decide(db, job):
    rows = db.execute('SELECT * FROM review_entry WHERE job=?', (job,)).fetchall()
    negative = sum(row['rating'] <= 2 for row in rows)
    return update(db, 'quality_case', job, state='review' if negative else 'clear',
                  reason='low public rating' if negative else 'no low public rating', negative_count=negative)
