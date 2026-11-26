from . import lineage
from .store import decision, row, rows, record


def inspect(db, jid):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not job:
        return record(db, 'audit', jid, 'missing', {})
    parents = lineage.declared_parents(db, jid)
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    attempt = row(db, 'SELECT * FROM attempts WHERE job=? ORDER BY generation DESC LIMIT 1', (jid,))
    proof = lineage.child_proof(db, jid)
    own_evidence = lineage.own_evidence(db, jid)

    # A delivery that already happened stays delivered: the pinned delivery
    # record, attempt generation and (when present) seal outlive the parent's
    # online bytes and any later cancellation of the parent.
    if artifact and artifact['status'] == 'purged' and own_evidence:
        decision_name = 'delivered'
    elif artifact and artifact['status'] == 'delivered':
        decision_name = 'delivered'
    elif job['status'] == 'cancelled':
        decision_name = 'cancelled'
    elif job['status'] == 'delivered' and own_evidence:
        decision_name = 'delivered'
    else:
        unmet = [p for p in parents if not lineage.parent_satisfied(db, p)]
        if unmet:
            decision_name = 'waiting_parents'
        elif job['status'] == 'breached':
            decision_name = 'breached'
        elif attempt and attempt['status'] == 'failed':
            decision_name = 'retry_needed'
        elif lease and lease['status'] == 'active':
            decision_name = 'running_or_reserved'
        else:
            decision_name = 'awaiting_slot'

    result = {
        'job_version': job['version'],
        'parents': parents,
        'parents_unmet': [p for p in parents if not lineage.parent_satisfied(db, p)],
        'seal_seqs': proof['seal_seqs'],
        'proof_complete': proof['complete'],
        'proof_authorized': proof['authorized'],
        'evidence_source': own_evidence['source'] if own_evidence else None,
        'artifact_status': artifact['status'] if artifact else None,
        'lease_generation': lease['generation'] if lease else None,
        'attempt_generation': attempt['generation'] if attempt else None,
        'stored_status': job['status'],
    }
    decision(db, 0, 'audit', jid, {'decision': decision_name, 'evidence': result})
    return record(db, 'audit', jid, decision_name, result)
