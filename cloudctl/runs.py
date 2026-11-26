import hashlib

from . import lineage
from .store import canonical_json, decision, row, rows, record


def _record_delivery(db, jid, digest, generation, at, source):
    """Pin the delivery event independently of the online artifact bytes.

    Append-only: once a delivery is recorded it is never rewritten, so a
    concurrent or stale receipt cannot erase an event that already happened.
    """
    existing = row(db, 'SELECT * FROM deliveries WHERE job=?', (jid,))
    if existing:
        return existing
    closure = lineage.build_delivery_closure(db, jid) or []
    snapshot = canonical_json({
        'source': source,
        'job_version': lineage.job_version(db, jid),
        'digest': digest,
        'generation': generation,
        'delivered_at': at,
        'closure': closure,
    })
    db.execute(
        'INSERT INTO deliveries(job,digest,at,generation,snapshot) VALUES(?,?,?,?,?)',
        (jid, digest, at, generation, snapshot),
    )
    return row(db, 'SELECT * FROM deliveries WHERE job=?', (jid,))


def _deliver(db, jid, digest, generation, at, source):
    db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)', (jid, digest, at, 'delivered'))
    db.execute('UPDATE jobs SET status=? WHERE id=?', ('delivered', jid))
    db.execute('UPDATE leases SET status=? WHERE job=?', ('released', jid))
    _record_delivery(db, jid, digest, generation, at, source)
    decision(db, at, 'delivery', jid, {'digest': digest, 'generation': generation, 'at': at, 'source': source})


def start(db, jid, generation, at):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if not job or job['status'] != 'accepted' or not lease or lease['status'] != 'active' or lease['generation'] != generation:
        return record(db, 'start', jid, 'not_leased', {'generation': generation})
    if at > lease['expires']:
        db.execute('UPDATE leases SET status=? WHERE job=?', ('expired', jid))
        return record(db, 'start', jid, 'lease_expired', {'expires': lease['expires'], 'at': at})
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (lease['slot'],))
    if not slot or not slot['healthy'] or at - slot['heartbeat'] > 60:
        db.execute('UPDATE leases SET status=? WHERE job=?', ('isolated', jid))
        return record(db, 'start', jid, 'slot_unavailable', {'slot': lease['slot'], 'at': at})
    existing = row(db, 'SELECT * FROM attempts WHERE job=? AND generation=?', (jid, generation))
    if existing:
        return record(db, 'start', jid, 'idempotent', {})
    db.execute('INSERT INTO attempts(job,generation,status,started) VALUES(?,?,?,?)', (jid, generation, 'running', at))
    return record(db, 'start', jid, 'running', {'generation': generation})


def settle(db, jid, generation, at, digest, success=True, content=None):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    attempt = row(db, 'SELECT * FROM attempts WHERE job=? AND generation=?', (jid, generation))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if not attempt or not lease or lease['generation'] != generation or lease['status'] != 'active':
        return record(db, 'settle', jid, 'stale_attempt', {'generation': generation})
    if attempt['status'] != 'running':
        return record(db, 'settle', jid, 'idempotent', {})
    if at > lease['expires']:
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND generation=?', ('expired', at, jid, generation))
        db.execute('UPDATE leases SET status=? WHERE job=?', ('expired', jid))
        return record(db, 'settle', jid, 'lease_expired', {'expires': lease['expires'], 'at': at})
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (lease['slot'],))
    if not slot or not slot['healthy'] or at - slot['heartbeat'] > 60:
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND generation=?', ('isolated', at, jid, generation))
        db.execute('UPDATE leases SET status=? WHERE job=?', ('isolated', jid))
        return record(db, 'settle', jid, 'slot_unavailable', {'slot': lease['slot'], 'at': at})
    if success and (content is None or digest != 'sha256:' + hashlib.sha256(content.encode()).hexdigest()):
        return record(db, 'settle', jid, 'digest_mismatch', {'generation': generation})
    db.execute('UPDATE attempts SET status=?,ended=?,digest=? WHERE job=? AND generation=?', ('success' if success else 'failed', at, digest, jid, generation))
    if not success:
        decision = 'retryable' if at + job['duration'] < job['deadline'] else 'breached'
        db.execute('UPDATE leases SET status=? WHERE job=?', ('failed', jid))
        if decision == 'breached':
            db.execute('UPDATE jobs SET status=? WHERE id=?', ('breached', jid))
        return record(db, 'settle', jid, decision, {'at': at, 'deadline': job['deadline']})
    unmet = [p['parent'] for p in rows(db, 'SELECT parent FROM dependencies WHERE job=?', (jid,)) if not lineage.parent_satisfied(db, p['parent'])]
    if unmet or at > job['deadline'] or not digest:
        return record(db, 'settle', jid, 'delivery_blocked', {'unmet': unmet, 'late': at > job['deadline']})
    _deliver(db, jid, digest, generation, at, 'settle')
    return record(db, 'settle', jid, 'delivered', {'digest': digest, 'at': at})


def finalize(db, jid, generation, at):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    attempt = row(db, 'SELECT * FROM attempts WHERE job=? AND generation=?', (jid, generation))
    if not job or not attempt or attempt['status'] != 'success':
        return record(db, 'finalize', jid, 'no_successful_attempt', {})
    if job['status'] not in ('accepted', 'delivered'):
        return record(db, 'finalize', jid, 'job_not_active', {'status': job['status']})
    if job['status'] == 'delivered':
        return record(db, 'finalize', jid, 'idempotent', {})
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if not lease or lease['generation'] != generation or lease['status'] != 'active':
        return record(db, 'finalize', jid, 'stale_attempt', {'generation': generation})
    if at > lease['expires']:
        db.execute('UPDATE leases SET status=? WHERE job=?', ('expired', jid))
        return record(db, 'finalize', jid, 'lease_expired', {'expires': lease['expires'], 'at': at})
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (lease['slot'],))
    if not slot or not slot['healthy'] or at - slot['heartbeat'] > 60:
        db.execute('UPDATE leases SET status=? WHERE job=?', ('isolated', jid))
        return record(db, 'finalize', jid, 'slot_unavailable', {'slot': lease['slot'], 'at': at})
    unmet = [p['parent'] for p in rows(db, 'SELECT parent FROM dependencies WHERE job=?', (jid,)) if not lineage.parent_satisfied(db, p['parent'])]
    if unmet:
        return record(db, 'finalize', jid, 'waiting_parents', {'unmet': unmet})
    if attempt['ended'] > job['deadline']:
        db.execute('UPDATE jobs SET status=? WHERE id=?', ('breached', jid))
        return record(db, 'finalize', jid, 'breached', {'finished': attempt['ended']})
    _deliver(db, jid, attempt['digest'], generation, at, 'finalize')
    return record(db, 'finalize', jid, 'delivered', {'digest': attempt['digest'], 'parent_count': len(declared(db, jid))})


def declared(db, jid):
    return rows(db, 'SELECT parent FROM dependencies WHERE job=?', (jid,))


def retention(db, jid, expires, hold, reason, at=None):
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=? AND status=?', (jid, 'delivered'))
    if not artifact:
        return record(db, 'retention', jid, 'not_delivered', {})
    db.execute('INSERT OR REPLACE INTO retentions VALUES(?,?,?,?)', (jid, expires, int(hold), reason))
    decision(db, at, 'retention', jid, {'expires': expires, 'hold': int(hold), 'reason': reason})
    return record(db, 'retention', jid, 'held' if hold else 'retained', {'expires': expires, 'hold': int(hold)})


def _protected_descendants(db, jid, at):
    """In-force protected delivered descendants that still lack a complete seal."""
    blocked = []
    for child in lineage.descendants(db, jid):
        retention = row(db, 'SELECT * FROM retentions WHERE job=?', (child,))
        if not retention or not retention['hold'] or retention['expires'] <= at:
            continue
        if not row(db, 'SELECT * FROM artifacts WHERE job=? AND status=?', (child, 'delivered')):
            continue
        proof = lineage.child_proof(db, child)
        if not proof['complete'] or not proof['authorized']:
            blocked.append({'child': child, 'errors': proof['errors'], 'proof_complete': proof['complete'], 'expires': retention['expires']})
    return blocked


def purge(db, at):
    """Release online parent bytes only when every protected child is proven.

    The candidate's own retention gives the window; it never keeps a copy merely
    because a descendant's window is longer.  Each in-force protected, delivered
    descendant must carry a verified, authorizing seal before the bytes go.
    """
    candidates = rows(db, 'SELECT job,expires,hold FROM retentions WHERE expires<=? AND hold=0', (at,))
    released = []
    deferred = []
    for item in candidates:
        jid = item['job']
        artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
        if not artifact or artifact['status'] != 'delivered':
            continue
        blockers = _protected_descendants(db, jid, at)
        if blockers:
            deferred.append({'job': jid, 'blocked_by': blockers})
            decision(db, at, 'purge', jid, {
                'decision': 'deferred',
                'reason': 'protected_descendant_unproven',
                'blocked_by': blockers,
            })
            record(db, 'purge', jid, 'deferred', {'at': at, 'blocked_by': [b['child'] for b in blockers]})
            continue
        db.execute('UPDATE artifacts SET status=? WHERE job=?', ('purged', jid))
        db.execute('INSERT OR REPLACE INTO purges(job,at,decision,evidence) VALUES(?,?,?,?)', (
            jid, at, 'released', canonical_json({'at': at, 'retention_expires': item['expires'], 'lineage': 'sealed'}),
        ))
        decision(db, at, 'purge', jid, {'decision': 'released', 'retention_expires': item['expires']})
        record(db, 'purge', jid, 'purged', {'at': at})
        released.append(jid)
    purge_decision = 'purged'
    if released and deferred:
        purge_decision = 'partially_purged'
    elif deferred:
        purge_decision = 'deferred'
    return {'decision': purge_decision, 'jobs': released, 'deferred': deferred}


def verify_lineage(db, child):
    """Independent audit surface: re-verify seals without the parent's bytes."""
    proof = lineage.child_proof(db, child)
    seals = lineage.verified_seals(db, child)
    evidence = lineage.evidence(db, child)
    decision(db, 0, 'verify', child, {'complete': proof['complete'], 'authorized': proof['authorized'], 'seal_seqs': proof['seal_seqs']})
    return record(db, 'verify', child, 'lineage_verified' if proof['complete'] else 'lineage_incomplete', {
        'proof': proof,
        'evidence': evidence,
        'seal_count': len(seals),
    })
