"""Quality case derived from independently attributed review claims.

Only root claims count toward the independent negative total: mirror pages
confirmed or tentatively merged through a signed repost mapping count as one
with their origin, and claims still under identity verification are reported
as pending instead of being counted as proven service failures.
"""

from .store import update


def decide(db, job):
    rows = db.execute('SELECT * FROM review_entry WHERE job=? ORDER BY platform,review_key', (job,)).fetchall()
    mirrors = {}
    for row in rows:
        if row['merged_into'] is not None:
            mirrors.setdefault(row['merged_into'], []).append(f"{row['platform']}:{row['review_key']}")
    confirmed = []
    for row in rows:
        if row['merged_into'] is not None or row['pending'] or row['retracted']:
            continue
        if row['rating'] is None or row['rating'] > 2:
            continue
        label = f"{row['platform']}:{row['review_key']}"
        copies = mirrors.get(f"{row['platform']}:{row['review_key']}:{job}")
        if copies:
            label += f"(mirrors:{','.join(sorted(copies))})"
        confirmed.append(label)
    pending = db.execute("SELECT COUNT(*) FROM identity_case WHERE job=? AND state='open'", (job,)).fetchone()[0]
    negative = len(confirmed)
    if negative:
        reason = f"{negative} confirmed independent low rating(s): {', '.join(confirmed)}"
    else:
        reason = 'no confirmed independent low rating'
    if pending:
        reason += f'; {pending} claim(s) pending identity verification'
    state = 'review' if negative else ('verify' if pending else 'clear')
    return update(db, 'quality_case', job, state=state, reason=reason,
                  negative_count=negative, pending_count=pending)
