"""Lineage evidence for delivered artifacts.

A child artifact must be auditable without its parent's online bytes.  A
sealed proof pins the parent digest, the successful attempt generation, the
delivery event and the transitive dependency/version closure, and is signed
over the exact persisted payload.  Seals are append-only; verification
re-derives everything that is still available from the retained metadata
tables (jobs, dependencies, attempts, deliveries, artifacts) and rejects
payloads that omit an attempt generation or disagree with a recorded digest.
"""

import hmac
import json

from .store import canonical_json, decision, record, row, rows, signature

SCHEMA_VERSION = 'lineage-v1'
DIGEST_PREFIX = 'sha256:'


def _digest_ok(value):
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return False
    body = value[len(DIGEST_PREFIX):]
    return len(body) == 64 and all(c in '0123456789abcdef' for c in body)


def _int_ok(value):
    return isinstance(value, int) and not isinstance(value, bool)


def ancestors(db, jid):
    """Transitive parents in a deterministic order, cycle safe."""
    order = []
    seen = set()
    frontier = [x['parent'] for x in rows(db, 'SELECT parent FROM dependencies WHERE job=? ORDER BY parent', (jid,))]
    while frontier:
        batch = sorted(set(frontier) - seen)
        frontier = []
        for parent in batch:
            seen.add(parent)
            order.append(parent)
            frontier.extend(x['parent'] for x in rows(db, 'SELECT parent FROM dependencies WHERE job=? ORDER BY parent', (parent,)))
    return order


def declared_parents(db, jid):
    return sorted(x['parent'] for x in rows(db, 'SELECT parent FROM dependencies WHERE job=?', (jid,)))


def descendants(db, jid):
    """Transitive children in a deterministic, cycle-safe order."""
    order = []
    seen = set()
    frontier = [x['job'] for x in rows(db, 'SELECT job FROM dependencies WHERE parent=? ORDER BY job', (jid,))]
    while frontier:
        batch = sorted(set(frontier) - seen)
        frontier = []
        for child in batch:
            seen.add(child)
            order.append(child)
            frontier.extend(x['job'] for x in rows(db, 'SELECT job FROM dependencies WHERE parent=? ORDER BY job', (child,)))
    return order


def _attempt_for(db, jid, generation):
    if generation is None:
        return None
    found = row(db, 'SELECT * FROM attempts WHERE job=? AND generation=?', (jid, generation))
    if not found or found['status'] != 'success' or not _digest_ok(found['digest']):
        return None
    return found


def _delivery_material(db, jid):
    """Material pinned by an immutable delivery record plus its attempt."""
    delivery = row(db, 'SELECT * FROM deliveries WHERE job=?', (jid,))
    if not delivery:
        return None
    attempt = _attempt_for(db, jid, delivery['generation'])
    if not attempt or attempt['digest'] != delivery['digest']:
        return None
    return {
        'digest': delivery['digest'],
        'generation': delivery['generation'],
        'delivered_at': delivery['at'],
        'attempt_started': attempt['started'],
        'attempt_ended': attempt['ended'],
    }


def _backfill_delivery(db, jid):
    """Persist a delivery record for artifacts delivered by an older build."""
    existing = row(db, 'SELECT * FROM deliveries WHERE job=?', (jid,))
    if existing:
        return existing
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
    if not artifact or not _digest_ok(artifact['digest']):
        return None
    attempt = row(
        db,
        "SELECT * FROM attempts WHERE job=? AND status='success' ORDER BY generation DESC LIMIT 1",
        (jid,),
    )
    if not attempt or attempt['digest'] != artifact['digest']:
        return None
    snapshot = canonical_json({'reconstructed': True, 'artifact': artifact['digest']})
    db.execute(
        'INSERT INTO deliveries(job,digest,at,generation,snapshot) VALUES(?,?,?,?,?)',
        (jid, artifact['digest'], artifact['delivered'], attempt['generation'], snapshot),
    )
    return _delivery_material(db, jid)


def job_version(db, jid):
    job = row(db, 'SELECT version FROM jobs WHERE id=?', (jid,))
    return job['version'] if job else None


def delivered_version(db, jid):
    """Job version captured at delivery time; immutable once the artifact is sealed.

    Current ``jobs.version`` may be bumped later by a cancellation or revision,
    which must not retroactively invalidate evidence of a delivery that already
    happened.  The delivery snapshot is the historical anchor when available.
    """
    delivery = row(db, 'SELECT snapshot FROM deliveries WHERE job=?', (jid,))
    if not delivery:
        return None
    try:
        snapshot = json.loads(delivery['snapshot'])
    except (TypeError, ValueError):
        return None
    value = snapshot.get('job_version')
    return value if _int_ok(value) and value >= 1 else None


def anchored_version(db, jid):
    """Version actually used to describe a delivery (snapshot first, then live)."""
    return delivered_version(db, jid) or job_version(db, jid)


def live_node(db, jid):
    """Node material while the artifact bytes are still online."""
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
    if not artifact or artifact['status'] != 'delivered' or not _digest_ok(artifact['digest']):
        return None
    material = _delivery_material(db, jid) or _backfill_delivery(db, jid)
    if not material:
        return None
    version = anchored_version(db, jid)
    if version is None:
        return None
    return {
        'job': jid,
        'version': version,
        'digest': artifact['digest'],
        'generation': material['generation'],
        'delivered_at': artifact['delivered'],
        'attempt_started': material.get('attempt_started'),
        'attempt_ended': material.get('attempt_ended'),
        'source': 'live',
    }


def verify_seal(db, seal):
    """Return (payload, errors) for a persisted seal row."""
    errors = []
    try:
        payload = json.loads(seal['payload'])
    except (TypeError, ValueError):
        return None, ['invalid_payload']
    recomputed = signature(db, seal['payload'])
    if not hmac.compare_digest(recomputed, seal['signature'] or ''):
        errors.append('signature_mismatch')
    if payload.get('schema') != SCHEMA_VERSION:
        errors.append('schema_mismatch')
    if payload.get('child') != seal['child'] or payload.get('parent') != seal['parent']:
        errors.append('edge_mismatch')
    if not _int_ok(payload.get('sealed_at')) or payload['sealed_at'] != seal['sealed_at']:
        errors.append('sealed_at_mismatch')
    if payload.get('authorize_parent_purge') is not True:
        errors.append('purge_not_authorized')
    declared = declared_parents(db, seal['child'])
    if sorted(payload.get('declared_parents') or []) != declared:
        errors.append('declared_parents_mismatch')
    delivery = payload.get('child_delivery') or {}
    attempt = payload.get('child_attempt') or {}
    if not _digest_ok(delivery.get('digest')) or not _int_ok(delivery.get('at')):
        errors.append('child_delivery_incomplete')
    if not _int_ok(attempt.get('generation')) or attempt.get('status') != 'success':
        errors.append('child_attempt_incomplete')
    if attempt.get('generation') != delivery.get('generation') or attempt.get('digest') != delivery.get('digest'):
        errors.append('attempt_generation_mismatch')
    recorded = _delivery_material(db, seal['child'])
    if not recorded:
        errors.append('child_delivery_unrecorded')
    elif (
        recorded['digest'] != delivery.get('digest')
        or recorded['generation'] != delivery.get('generation')
        or recorded['delivered_at'] != delivery.get('at')
    ):
        errors.append('child_delivery_mismatch')
    if _int_ok(delivery.get('at')) and _int_ok(payload.get('sealed_at')) and payload['sealed_at'] < delivery['at']:
        errors.append('sealed_before_delivery')
    closure = payload.get('closure')
    if not isinstance(closure, list):
        return payload, errors + ['closure_missing']
    if sorted(x.get('job') for x in closure if isinstance(x, dict)) != sorted(ancestors(db, seal['child'])):
        errors.append('closure_jobs_mismatch')
    if len({x.get('job') for x in closure if isinstance(x, dict)}) != len(closure):
        errors.append('closure_duplicate')
    for node in closure:
        if not isinstance(node, dict):
            errors.append('closure_node_invalid')
            continue
        jid = node.get('job')
        if not _digest_ok(node.get('digest')):
            errors.append('closure_digest_invalid:' + str(jid))
        if not _int_ok(node.get('generation')) or node.get('attempt_status') != 'success':
            errors.append('closure_attempt_incomplete:' + str(jid))
        if not _int_ok(node.get('delivered_at')):
            errors.append('closure_delivery_missing:' + str(jid))
        if node.get('version') != anchored_version(db, jid):
            errors.append('closure_version_mismatch:' + str(jid))
        recorded = _delivery_material(db, jid)
        if not recorded:
            errors.append('closure_delivery_unrecorded:' + str(jid))
            continue
        if (
            recorded['digest'] != node.get('digest')
            or recorded['generation'] != node.get('generation')
            or recorded['delivered_at'] != node.get('delivered_at')
        ):
            errors.append('closure_delivery_mismatch:' + str(jid))
        attempt = _attempt_for(db, jid, node.get('generation'))
        if not attempt or attempt['digest'] != node.get('digest'):
            errors.append('closure_attempt_mismatch:' + str(jid))
        artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
        if not artifact:
            errors.append('closure_artifact_missing:' + str(jid))
        elif artifact['status'] == 'delivered' and artifact['digest'] != node.get('digest'):
            errors.append('closure_live_digest_mismatch:' + str(jid))
    return payload, errors


def verified_seals(db, child=None):
    """All seals that pass verification, newest first."""
    sql = 'SELECT * FROM lineage_seals'
    args = ()
    if child is not None:
        sql += ' WHERE child=?'
        args = (child,)
    sql += ' ORDER BY seq DESC'
    good = []
    for seal in rows(db, sql, args):
        payload, errors = verify_seal(db, seal)
        if not errors:
            good.append({'row': seal, 'payload': payload})
    return good


def evidence(db, jid):
    """Digest/attempt/delivery evidence for a job: live bytes first, else seals."""
    live = live_node(db, jid)
    if live:
        return live
    for seal in verified_seals(db):
        payload = seal['payload']
        if payload.get('child') == jid:
            attempt = payload.get('child_attempt') or {}
            delivery = payload.get('child_delivery') or {}
            return {
                'job': jid,
                'version': anchored_version(db, jid),
                'digest': delivery.get('digest'),
                'generation': delivery.get('generation'),
                'delivered_at': delivery.get('at'),
                'attempt_started': attempt.get('started'),
                'attempt_ended': attempt.get('ended'),
                'source': 'sealed',
                'seal_seq': seal['row']['seq'],
            }
        for node in payload.get('closure') or []:
            if node.get('job') == jid:
                return {
                    'job': jid,
                    'version': node.get('version'),
                    'digest': node.get('digest'),
                    'generation': node.get('generation'),
                    'delivered_at': node.get('delivered_at'),
                    'attempt_started': node.get('attempt_started'),
                    'attempt_ended': node.get('attempt_ended'),
                    'source': 'sealed',
                    'seal_seq': seal['row']['seq'],
                }
    return None


def pinned_evidence(db, jid):
    """Delivery evidence that survives purging the job's own online bytes.

    Only the immutable delivery record and its successful attempt are consulted;
    it is used for auditing a job's own past delivery, never to satisfy a
    pending child's dependency (which requires live bytes or a verified seal).
    """
    material = _delivery_material(db, jid) or _backfill_delivery(db, jid)
    if not material:
        return None
    pinned = {
        'job': jid,
        'version': anchored_version(db, jid),
        'digest': material['digest'],
        'generation': material['generation'],
        'delivered_at': material['delivered_at'],
        'attempt_started': material.get('attempt_started'),
        'attempt_ended': material.get('attempt_ended'),
        'source': 'pinned',
    }
    return pinned


def own_evidence(db, jid):
    """Best evidence that jid's delivery happened, regardless of online bytes."""
    return evidence(db, jid) or pinned_evidence(db, jid)


def parent_satisfied(db, parent):
    """A dependency is met by online bytes or by sealed lineage evidence."""
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (parent,))
    if not job:
        return False
    return evidence(db, parent) is not None


def build_delivery_closure(db, child):
    """Snapshot retained at delivery time for every delivered child.

    Records each ancestor's digest, successful attempt generation, delivery
    event and job version.  It is the baseline that a signed seal later
    corroborates; it never substitutes for a seal when a parent must be purged.
    """
    nodes = []
    for parent in ancestors(db, child):
        material = evidence(db, parent)
        if not material:
            return None
        nodes.append(_node_payload(material))
    return sorted(nodes, key=lambda x: x['job'])


def _node_payload(material):
    return {
        'job': material['job'],
        'version': material['version'],
        'digest': material['digest'],
        'generation': material['generation'],
        'delivered_at': material['delivered_at'],
        'attempt_status': 'success',
        'attempt_started': material.get('attempt_started'),
        'attempt_ended': material.get('attempt_ended'),
        'source_at_seal': material['source'],
    }


def build_snapshot(db, child, at):
    """Closure payload for a delivered child, or (None, missing jobs)."""
    material = live_node(db, child)
    if not material:
        return None, {'child': child, 'missing': [child], 'reason': 'child_not_live'}
    missing = []
    nodes = []
    for parent in ancestors(db, child):
        parent_material = evidence(db, parent)
        if not parent_material:
            missing.append(parent)
            continue
        # A seal must pin the generation of a *successful* attempt for each
        # ancestor.  A later record that lost that attempt cannot be sealed.
        if not _digest_ok(parent_material.get('digest')) or not _int_ok(parent_material.get('generation')):
            missing.append(parent)
            continue
        if _attempt_for(db, parent, parent_material.get('generation')) is None:
            missing.append(parent)
            continue
        nodes.append(_node_payload(parent_material))
    if missing:
        return None, {'child': child, 'missing': missing, 'reason': 'incomplete_lineage'}
    payload = {
        'schema': SCHEMA_VERSION,
        'child': child,
        'child_version': material['version'],
        'child_delivery': {
            'digest': material['digest'],
            'generation': material['generation'],
            'at': material['delivered_at'],
        },
        'child_attempt': {
            'job': child,
            'generation': material['generation'],
            'status': 'success',
            'started': material.get('attempt_started'),
            'ended': material.get('attempt_ended'),
            'digest': material['digest'],
        },
        'declared_parents': declared_parents(db, child),
        'closure': sorted(nodes, key=lambda x: x['job']),
        'sealed_at': at,
        'authorize_parent_purge': True,
    }
    return payload, None


def seal(db, child, at):
    """Seal every declared parent edge of a delivered child."""
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (child,))
    if not job:
        return record(db, 'seal', child, 'missing', {})
    if not row(db, 'SELECT * FROM artifacts WHERE job=? AND status=?', (child, 'delivered')):
        return record(db, 'seal', child, 'not_delivered', {})
    payload, missing = build_snapshot(db, child, at)
    if not payload:
        decision_payload = {'reason': missing['reason'], 'missing': missing['missing']}
        decision(db, at, 'seal', child, {'decision': 'rejected', **decision_payload})
        return record(db, 'seal', child, 'incomplete_lineage', decision_payload)
    inserted = []
    for parent in payload['declared_parents']:
        edge_payload = dict(payload)
        edge_payload['parent'] = parent
        payload_text = canonical_json(edge_payload)
        sig = signature(db, payload_text)
        existing = row(
            db,
            'SELECT * FROM lineage_seals WHERE child=? AND parent=? AND payload=? AND signature=?',
            (child, parent, payload_text, sig),
        )
        if existing:
            continue
        db.execute(
            'INSERT INTO lineage_seals(child,parent,sealed_at,payload,signature) VALUES(?,?,?,?,?)',
            (child, parent, at, payload_text, sig),
        )
        inserted.append(parent)
    result = {
        'decision': 'sealed',
        'parents': payload['declared_parents'],
        'inserted': inserted,
        'closure': [x['job'] for x in payload['closure']],
    }
    decision(db, at, 'seal', child, result)
    return record(db, 'seal', child, 'sealed' if payload['declared_parents'] else 'sealed_trivial', result)


def child_proof(db, child):
    """Whether every declared parent edge has a valid authorizing seal."""
    parents = declared_parents(db, child)
    seals = verified_seals(db, child)
    by_parent = {}
    for seal in seals:
        if seal['payload'].get('parent') in parents:
            by_parent.setdefault(seal['payload']['parent'], seal)
    errors = []
    nodes = []
    authorized = True
    for parent in parents:
        seal = by_parent.get(parent)
        if not seal:
            errors.append('seal_missing:' + parent)
            authorized = False
            continue
        if seal['payload'].get('authorize_parent_purge') is not True:
            authorized = False
        nodes.extend(x['job'] for x in seal['payload'].get('closure') or [])
    delivery = _delivery_material(db, child) or _backfill_delivery(db, child)
    if not delivery:
        errors.append('delivery_unrecorded')
    return {
        'job': child,
        'complete': not errors,
        'authorized': authorized and not errors,
        'parents': parents,
        'seal_seqs': {p: s['row']['seq'] for p, s in by_parent.items()},
        'closure': sorted(set(nodes)),
        'errors': errors,
        'delivery': delivery,
    }
