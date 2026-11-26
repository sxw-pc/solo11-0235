"""Key rotation and emergency revocation control for job secret mounts.

Raw key bytes never touch the local database or event log: only envelope
identifiers, sealed ciphertext digests, external key-service receipts,
container attestations and artifact digests are persisted.

A mount binds tenant, job, lease generation, key version and a short validity
window.  Rotation cannot hand write capability to a second machine until the
previous mount is provably reclaimed by the key service, its lease is
isolated, or it has expired.  Revocation reasons, key-service confirmations,
run attempts and artifact digests form a replayable evidence chain, and any
artifact that read a leaked version is ineligible for customer delivery even
after a safe version is remounted.
"""
import uuid

from .store import row, rows, record

MOUNT_TTL_CAP = 300

# Mount statuses:
#   active            - usable right now, at most one per job
#   reclaimed         - key service confirmed remote reclamation
#   revoked_pending   - version revoked, awaiting reclamation evidence
#   isolated          - slot went unhealthy, lease isolated
#   quarantined       - temp bytes condemned, lease isolated for re-execution
#   expired           - short validity window elapsed
#   cancelled         - customer cancelled the job
TERMINAL_MOUNTS = ('reclaimed', 'expired', 'quarantined', 'cancelled')


def _ensure_tables(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS secret_envelope_versions(envelope_id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, tenant TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, sealed_digest TEXT NOT NULL, status TEXT NOT NULL, rotated_from INTEGER, registered_at INTEGER NOT NULL, UNIQUE(job,key_id,version));
        CREATE TABLE IF NOT EXISTS secret_revocations(id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL, version INTEGER NOT NULL, tenant TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, at INTEGER NOT NULL, UNIQUE(key_id,version,reason));
        CREATE TABLE IF NOT EXISTS secret_mounts(mount_id TEXT PRIMARY KEY, job TEXT NOT NULL, tenant TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, lease_generation INTEGER NOT NULL, slot TEXT, issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, ttl INTEGER NOT NULL, status TEXT NOT NULL, revocation_id INTEGER, recovery_id INTEGER, envelope_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS kms_revocations(receipt_id TEXT PRIMARY KEY, key_id TEXT NOT NULL, version INTEGER NOT NULL, mount_id TEXT, slot TEXT, action TEXT NOT NULL, confirmed_at INTEGER NOT NULL, duplicated INTEGER NOT NULL DEFAULT 0, claim TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS container_attestations(attestation_id TEXT PRIMARY KEY, slot TEXT NOT NULL, job TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, verdict TEXT NOT NULL, issued_at INTEGER NOT NULL, claim TEXT NOT NULL, UNIQUE(slot,job,key_id,version));
        CREATE TABLE IF NOT EXISTS rotation_intents(intent_id TEXT PRIMARY KEY, job TEXT NOT NULL, key_id TEXT NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, at INTEGER NOT NULL, decision TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery_points(recovery_id TEXT PRIMARY KEY, job TEXT NOT NULL, lease_generation INTEGER NOT NULL, slot TEXT NOT NULL, key_id TEXT NOT NULL, version INTEGER NOT NULL, point_digest TEXT NOT NULL, tainted INTEGER NOT NULL, created_at INTEGER NOT NULL, parent_recovery_id TEXT);
        CREATE TABLE IF NOT EXISTS attempt_lineage(lineage_seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, generation INTEGER NOT NULL, mount_id TEXT, key_id TEXT, version INTEGER, taint INTEGER NOT NULL DEFAULT 0, recovery_id TEXT, UNIQUE(job,generation,mount_id));
        CREATE TABLE IF NOT EXISTS delivery_eligibility(job TEXT PRIMARY KEY, eligible INTEGER NOT NULL, tainted_versions TEXT NOT NULL, evidence TEXT NOT NULL, decided_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS clean_recomputations(job TEXT NOT NULL, generation INTEGER NOT NULL, confirmed_at INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(job,generation));
        """
    )


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _expire_due_mounts(db, at):
    for m in rows(db, "SELECT mount_id FROM secret_mounts WHERE status='active' AND expires_at<=?", (at,)):
        db.execute("UPDATE secret_mounts SET status='expired' WHERE mount_id=?", (m['mount_id'],))


def _current_envelope(db, job, key_id=None):
    sql = "SELECT * FROM secret_envelope_versions WHERE job=? AND status='current'"
    args = [job]
    if key_id:
        sql += " AND key_id=?"
        args.append(key_id)
    sql += " ORDER BY version DESC LIMIT 1"
    return row(db, sql, tuple(args))


def _active_mount(db, job):
    return row(db, "SELECT * FROM secret_mounts WHERE job=? AND status='active' ORDER BY issued_at DESC LIMIT 1", (job,))


def _revoked_versions(db, key_id, tenant=None):
    sql = "SELECT DISTINCT version FROM secret_revocations WHERE key_id=? AND status='active'"
    args = [key_id]
    if tenant:
        sql += " AND tenant=?"
        args.append(tenant)
    return {r['version'] for r in rows(db, sql, tuple(args))}


# --------------------------------------------------------------------------- #
# Envelope registration and rotation
# --------------------------------------------------------------------------- #

def register_envelope(db, job, key_id, version, sealed_digest, at):
    """Register a sealed envelope version. No plaintext key material accepted."""
    _ensure_tables(db)
    job_row = row(db, "SELECT id,tenant FROM jobs WHERE id=?", (job,))
    if not job_row:
        return record(db, 'secret_envelope', job, 'missing_job', {})
    tenant = job_row['tenant']
    existing = row(db, "SELECT * FROM secret_envelope_versions WHERE job=? AND key_id=? AND version=?",
                   (job, key_id, version))
    if existing:
        if existing['sealed_digest'] == sealed_digest:
            return record(db, 'secret_envelope', job, 'idempotent',
                          {'key_id': key_id, 'version': version, 'status': existing['status']})
        return record(db, 'secret_envelope', job, 'conflict',
                      {'key_id': key_id, 'version': version, 'stored_digest': existing['sealed_digest']})
    prior = row(db, "SELECT * FROM secret_envelope_versions WHERE job=? AND key_id=? ORDER BY version DESC LIMIT 1",
                (job, key_id))
    if prior and prior['version'] >= version:
        return record(db, 'secret_envelope', job, 'conflict',
                      {'key_id': key_id, 'version': version, 'current_version': prior['version']})
    if prior:
        db.execute("UPDATE secret_envelope_versions SET status='superseded' WHERE envelope_id=?",
                   (prior['envelope_id'],))
    cur = db.execute(
        "INSERT INTO secret_envelope_versions(job,tenant,key_id,version,sealed_digest,status,rotated_from,registered_at)"
        " VALUES(?,?,?,?,?,'current',?,?)",
        (job, tenant, key_id, version, sealed_digest, prior['envelope_id'] if prior else None, at))
    # Legacy one-row table stays in sync for older readers.
    db.execute("CREATE TABLE IF NOT EXISTS secret_envelopes (job TEXT PRIMARY KEY, key_id TEXT NOT NULL,"
               " version INTEGER NOT NULL, sealed_digest TEXT NOT NULL)")
    db.execute("INSERT OR REPLACE INTO secret_envelopes VALUES(?,?,?,?)",
               (job, key_id, version, sealed_digest))
    return record(db, 'secret_envelope', job, 'registered',
                  {'key_id': key_id, 'version': version, 'tenant': tenant,
                   'superseded': prior['version'] if prior else None,
                   'envelope_id': cur.lastrowid})


def rotate(db, job, key_id, new_version, sealed_digest, at, reason='key_service_rotation'):
    """Issue a rotation intent and register the new envelope version.

    Rotation by itself never issues a mount: issuance stays gated on
    reclamation / isolation / expiry evidence for the old mount.
    """
    _ensure_tables(db)
    envelope = _current_envelope(db, job, key_id)
    if not envelope:
        return record(db, 'secret_rotation', job, 'no_envelope', {'key_id': key_id})
    if envelope['version'] >= new_version:
        return record(db, 'secret_rotation', job, 'conflict',
                      {'key_id': key_id, 'requested': new_version, 'current': envelope['version']})
    reg = register_envelope(db, job, key_id, new_version, sealed_digest, at)
    if reg['decision'] not in ('registered', 'idempotent'):
        return record(db, 'secret_rotation', job, reg['decision'], reg['evidence'])
    outstanding = _active_mount(db, job)
    status = 'awaiting_reclaim' if outstanding else 'ready'
    intent_id = _new_id('rot')
    db.execute("INSERT INTO rotation_intents(intent_id,job,key_id,from_version,to_version,reason,status,at,decision)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (intent_id, job, key_id, envelope['version'], new_version, reason, status, at,
                'blocked' if outstanding else 'proceed'))
    return record(db, 'secret_rotation', job, 'rotated' if outstanding else 'rotation_ready',
                  {'intent_id': intent_id, 'key_id': key_id, 'from_version': envelope['version'],
                   'to_version': new_version, 'outstanding_mount': outstanding['mount_id'] if outstanding else None,
                   'gate': status})


# --------------------------------------------------------------------------- #
# Emergency revocation (tenant scoped)
# --------------------------------------------------------------------------- #

def revoke(db, key_id, version, reason, at, tenant=None):
    """Mark a key version revoked and freeze every mount that still uses it.

    Only envelopes belonging to the revoked tenant's jobs are affected, so a
    revocation never crosses a parent/child dependency into another tenant.
    """
    _ensure_tables(db)
    env_sql = "SELECT * FROM secret_envelope_versions WHERE key_id=? AND version=?"
    env_args = [key_id, version]
    if tenant:
        env_sql += " AND tenant=?"
        env_args.append(tenant)
    envelopes = rows(db, env_sql, tuple(env_args))
    if not envelopes:
        return record(db, 'secret_revoke', key_id, 'unknown_version',
                      {'key_id': key_id, 'version': version, 'tenant': tenant})
    rev = row(db, "SELECT * FROM secret_revocations WHERE key_id=? AND version=? AND reason=?",
              (key_id, version, reason))
    if rev:
        rev_id = rev['id']
    else:
        cur = db.execute("INSERT INTO secret_revocations(key_id,version,tenant,reason,status,at)"
                         " VALUES(?,?,?,?,'active',?)",
                         (key_id, version, tenant or envelopes[0]['tenant'], reason, at))
        rev_id = cur.lastrowid
    affected_jobs = []
    for env in envelopes:
        db.execute("UPDATE secret_envelope_versions SET status='revoked' WHERE envelope_id=?",
                   (env['envelope_id'],))
        jid = env['job']
        affected_jobs.append(jid)
        for m in rows(db, "SELECT * FROM secret_mounts WHERE job=? AND key_id=? AND version=? AND status='active'",
                      (jid, key_id, version)):
            db.execute("UPDATE secret_mounts SET status='revoked_pending', revocation_id=? WHERE mount_id=?",
                       (rev_id, m['mount_id']))
        # Reads of this version are tainted until attestation + reclaim cure them.
        db.execute("UPDATE attempt_lineage SET taint=1 WHERE job=? AND key_id=? AND version=?",
                   (jid, key_id, version))
        db.execute("DELETE FROM delivery_eligibility WHERE job=?", (jid,))
    return record(db, 'secret_revoke', key_id, 'revoked',
                  {'revocation_id': rev_id, 'key_id': key_id, 'version': version, 'reason': reason,
                   'tenant': tenant, 'jobs': sorted(set(affected_jobs))})


# --------------------------------------------------------------------------- #
# External key service receipts (idempotent)
# --------------------------------------------------------------------------- #

def kms_receipt(db, receipt_id, key_id, version, action, at, mount_id=None, slot=None, claim=''):
    """Record a key-service receipt.

    A timeout receipt is evidence of uncertainty, never of reclamation: only
    an explicit reclaim/revoke confirmation releases the single-writer gate.
    Duplicate receipt IDs are recorded as duplicates without re-mutating state.
    """
    _ensure_tables(db)
    prior = row(db, "SELECT * FROM kms_revocations WHERE receipt_id=?", (receipt_id,))
    if prior:
        if not prior['duplicated']:
            db.execute("UPDATE kms_revocations SET duplicated=1 WHERE receipt_id=?", (receipt_id,))
        record(db, 'kms_receipt', key_id, 'duplicate_receipt',
               {'receipt_id': receipt_id, 'original_action': prior['action'], 'key_id': key_id,
                'version': version})
        return {'subject': key_id, 'decision': 'duplicate_receipt',
                'evidence': {'receipt_id': receipt_id, 'original_action': prior['action']}}
    db.execute("INSERT INTO kms_revocations VALUES(?,?,?,?,?,?,?,0,?)",
               (receipt_id, key_id, version, mount_id, slot, action, at, claim))
    if action in ('reclaim', 'revoke'):
        if mount_id:
            m = row(db, "SELECT * FROM secret_mounts WHERE mount_id=?", (mount_id,))
            if m and m['status'] in ('active', 'revoked_pending', 'isolated'):
                db.execute("UPDATE secret_mounts SET status='reclaimed' WHERE mount_id=?", (mount_id,))
        else:
            db.execute("UPDATE secret_mounts SET status='reclaimed' WHERE key_id=? AND version=?"
                       " AND status IN ('active','revoked_pending','isolated')", (key_id, version))
        for intent in rows(db, "SELECT * FROM rotation_intents WHERE key_id=? AND from_version=?"
                           " AND status='awaiting_reclaim'", (key_id, version)):
            blocked = row(db, "SELECT mount_id FROM secret_mounts WHERE job=? AND status='active'",
                          (intent['job'],))
            if not blocked:
                db.execute("UPDATE rotation_intents SET status='reclaim_verified', decision='proceed'"
                           " WHERE intent_id=?", (intent['intent_id'],))
        return record(db, 'kms_receipt', key_id, 'reclaim_confirmed',
                      {'receipt_id': receipt_id, 'key_id': key_id, 'version': version,
                       'mount_id': mount_id, 'action': action})
    # timeout / unknown: kept as evidence but releases nothing.
    return record(db, 'kms_receipt', key_id, 'receipt_recorded',
                  {'receipt_id': receipt_id, 'key_id': key_id, 'version': version, 'action': action,
                   'gate_released': False})


# --------------------------------------------------------------------------- #
# Container attestations
# --------------------------------------------------------------------------- #

def attest(db, attestation_id, slot, job, key_id, version, verdict, at, claim=''):
    _ensure_tables(db)
    if verdict not in ('unexposed', 'exposed'):
        return record(db, 'container_attestation', job, 'invalid_verdict', {'verdict': verdict})
    existing = row(db, "SELECT * FROM container_attestations WHERE slot=? AND job=? AND key_id=? AND version=?",
                   (slot, job, key_id, version))
    if existing:
        return record(db, 'container_attestation', job, 'idempotent',
                      {'attestation_id': existing['attestation_id'], 'verdict': existing['verdict']})
    db.execute("INSERT INTO container_attestations VALUES(?,?,?,?,?,?,?,?)",
               (attestation_id, slot, job, key_id, version, verdict, at, claim))
    if verdict == 'exposed':
        db.execute("UPDATE attempt_lineage SET taint=1 WHERE job=? AND key_id=? AND version=?",
                   (job, key_id, version))
        db.execute("DELETE FROM delivery_eligibility WHERE job=?", (job,))
    return record(db, 'container_attestation', job,
                  'container_unexposed' if verdict == 'unexposed' else 'container_exposed',
                  {'attestation_id': attestation_id, 'slot': slot, 'key_id': key_id, 'version': version})


# --------------------------------------------------------------------------- #
# Mount issuance: single writer, evidence gated
# --------------------------------------------------------------------------- #

def _non_active_mount_cleared(db, m, at):
    """Whether a no-longer-active mount releases the single-writer gate.

    The gate opens only on verifiable reclamation (key service), lease
    isolation/quarantine, or hard expiry.  A revoked-but-pending mount whose
    machine is merely silent keeps the gate shut.
    """
    if m['status'] in TERMINAL_MOUNTS:
        return True
    if m['status'] == 'isolated':
        # The mount itself being marked isolated is the lease-isolation evidence;
        # a newer lease generation may already exist for the job.
        return True
    if at > m['expires_at']:
        return True
    return False


def issue_mount(db, job, generation, at, ttl=MOUNT_TTL_CAP):
    """Issue a short-lived mount bound to tenant/job/generation/version/window."""
    _ensure_tables(db)
    _expire_due_mounts(db, at)
    envelope = _current_envelope(db, job)
    lease = row(db, "SELECT * FROM leases WHERE job=?", (job,))
    job_row = row(db, "SELECT id,tenant,status FROM jobs WHERE id=?", (job,))
    if not envelope:
        return record(db, 'secret_mount', job, 'unavailable', {'generation': generation, 'reason': 'no_envelope'})
    if not job_row or job_row['status'] != 'accepted':
        return record(db, 'secret_mount', job, 'unavailable',
                      {'generation': generation, 'reason': 'job_not_active'})
    if not lease or lease['status'] != 'active' or lease['generation'] != generation or at > lease['expires']:
        return record(db, 'secret_mount', job, 'unavailable',
                      {'generation': generation, 'reason': 'lease_check',
                       'lease_generation': lease['generation'] if lease else None,
                       'lease_status': lease['status'] if lease else None})
    active = _active_mount(db, job)
    if active:
        if (active['lease_generation'] == generation and active['version'] == envelope['version']
                and active['slot'] == lease['slot'] and at <= active['expires_at']):
            return record(db, 'secret_mount', job, 'idempotent',
                          {'mount_id': active['mount_id'], 'key_id': envelope['key_id'],
                           'version': envelope['version'], 'generation': generation})
        return record(db, 'secret_mount', job, 'dual_mount_blocked',
                      {'generation': generation, 'active_mount': active['mount_id'],
                       'active_slot': active['slot'], 'active_version': active['version']})
    if envelope['status'] == 'revoked' or envelope['version'] in _revoked_versions(
            db, envelope['key_id'], job_row['tenant']):
        return record(db, 'secret_mount', job, 'version_revoked',
                      {'generation': generation, 'key_id': envelope['key_id'],
                       'version': envelope['version']})
    # No active mount: every prior mount must be provably closed before a new writer.
    blockers = []
    for m in rows(db, "SELECT * FROM secret_mounts WHERE job=?", (job,)):
        if not _non_active_mount_cleared(db, m, at):
            blockers.append({'mount_id': m['mount_id'], 'status': m['status'], 'version': m['version']})
    if blockers:
        return record(db, 'secret_mount', job, 'awaiting_reclaim',
                      {'generation': generation, 'blockers': blockers})
    window = min(ttl, MOUNT_TTL_CAP, lease['expires'] - at)
    if window <= 0:
        return record(db, 'secret_mount', job, 'unavailable',
                      {'generation': generation, 'reason': 'window_closed'})
    mount_id = _new_id('m')
    db.execute("INSERT INTO secret_mounts(mount_id,job,tenant,key_id,version,lease_generation,slot,"
               "issued_at,expires_at,ttl,status,revocation_id,recovery_id,envelope_id)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,'active',NULL,NULL,?)",
               (mount_id, job, job_row['tenant'], envelope['key_id'], envelope['version'], generation,
                lease['slot'], at, at + window, window, envelope['envelope_id']))
    # Keep every read in the lineage.  A generation that re-mounts after rotation
    # (path A recovery on the same lease generation) keeps both version reads.
    db.execute("INSERT OR IGNORE INTO attempt_lineage(job,generation,mount_id,key_id,version,taint)"
               " VALUES(?,?,?,?,?,0)",
               (job, generation, mount_id, envelope['key_id'], envelope['version']))
    db.execute("UPDATE attempt_lineage SET mount_id=?, key_id=?, version=? WHERE job=? AND generation=?"
               " AND mount_id IS NULL",
               (mount_id, envelope['key_id'], envelope['version'], job, generation))
    db.execute("DELETE FROM delivery_eligibility WHERE job=?", (job,))
    return record(db, 'secret_mount', job, 'issued',
                  {'mount_id': mount_id, 'tenant': job_row['tenant'], 'key_id': envelope['key_id'],
                   'version': envelope['version'], 'generation': generation, 'slot': lease['slot'],
                   'issued_at': at, 'expires_at': at + window, 'ttl': window})


def mount_for_attempt(db, job, generation, at):
    """Gate used by run.start: the attempt must hold a live, non-revoked mount."""
    _ensure_tables(db)
    if not row(db, "SELECT 1 FROM secret_envelope_versions WHERE job=?", (job,)):
        return True, None  # job uses no customer secrets
    _expire_due_mounts(db, at)
    job_row = row(db, "SELECT tenant FROM jobs WHERE id=?", (job,))
    m = row(db, "SELECT * FROM secret_mounts WHERE job=? AND lease_generation=? AND status='active'",
            (job, generation))
    if not m or at > m['expires_at']:
        return False, 'secret_mount_required'
    if m['version'] in _revoked_versions(db, m['key_id'], job_row['tenant'] if job_row else None):
        return False, 'secret_version_revoked'
    return True, m


# --------------------------------------------------------------------------- #
# Recovery path A: proven unexposed container + reclaimed old mount
# --------------------------------------------------------------------------- #

def resume_at_recovery_point(db, job, at, point_digest, ttl=MOUNT_TTL_CAP):
    """Continue on the same job after rotation, preserving pre-rotation work.

    Requires both an unexposed-container attestation for the old version and a
    key-service reclamation confirmation; nothing less releases the gate.
    """
    _ensure_tables(db)
    envelope = _current_envelope(db, job)
    if not envelope:
        return record(db, 'recovery', job, 'no_envelope', {})
    old = row(db, "SELECT * FROM secret_mounts WHERE job=? AND version<>? ORDER BY issued_at DESC LIMIT 1",
              (job, envelope['version']))
    if not old:
        return record(db, 'recovery', job, 'no_prior_mount', {'key_id': envelope['key_id']})
    attestation = row(db, "SELECT * FROM container_attestations WHERE slot=? AND job=? AND key_id=? AND version=?"
                          " AND verdict='unexposed'",
                      (old['slot'], job, old['key_id'], old['version']))
    receipt = row(db, "SELECT 1 FROM kms_revocations WHERE key_id=? AND version=? AND action IN ('reclaim','revoke')",
                  (old['key_id'], old['version']))
    missing = []
    if not attestation:
        missing.append('container_attestation')
    if not receipt:
        missing.append('kms_reclaim_receipt')
    if missing:
        return record(db, 'recovery', job, 'evidence_incomplete',
                      {'missing': missing, 'old_mount': old['mount_id'], 'old_version': old['version']})
    lease = row(db, "SELECT * FROM leases WHERE job=? AND status='active'", (job,))
    if not lease or at > lease['expires']:
        return record(db, 'recovery', job, 'lease_unavailable', {})
    parent = row(db, "SELECT recovery_id FROM recovery_points WHERE job=? ORDER BY created_at DESC LIMIT 1", (job,))
    recovery_id = _new_id('rp')
    db.execute("INSERT INTO recovery_points(recovery_id,job,lease_generation,slot,key_id,version,"
               "point_digest,tainted,created_at,parent_recovery_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (recovery_id, job, lease['generation'], lease['slot'], envelope['key_id'],
                envelope['version'], point_digest, 0, at, parent['recovery_id'] if parent else None))
    # The attested pre-rotation read is cured: lineage keeps the read but untaints it.
    db.execute("UPDATE attempt_lineage SET taint=0 WHERE job=? AND key_id=? AND version=?",
               (job, old['key_id'], old['version']))
    issued = issue_mount(db, job, lease['generation'], at, ttl)
    if issued['decision'] == 'issued':
        db.execute("UPDATE attempt_lineage SET recovery_id=? WHERE job=? AND generation=?",
                   (recovery_id, job, lease['generation']))
        issued['evidence']['recovery_id'] = recovery_id
        issued['evidence']['resumed_from_version'] = old['version']
        issued['evidence']['attestation_id'] = attestation['attestation_id']
    return issued

# --------------------------------------------------------------------------- #
# Recovery path B: offline machine, quarantine and re-execution on standby
# --------------------------------------------------------------------------- #

def isolate_and_replan(db, job, at, reason='reclaim_unverified'):
    """Isolate the lease and condemn temp bytes that touched the old version."""
    _ensure_tables(db)
    lease = row(db, "SELECT * FROM leases WHERE job=?", (job,))
    if not lease:
        return record(db, 'secret_quarantine', job, 'no_lease', {})
    db.execute("UPDATE leases SET status='isolated' WHERE job=? AND status='active'", (job,))
    for m in rows(db, "SELECT * FROM secret_mounts WHERE job=? AND status IN ('active','revoked_pending','isolated')",
                  (job,)):
        db.execute("UPDATE secret_mounts SET status='quarantined' WHERE mount_id=?", (m['mount_id'],))
        db.execute("UPDATE attempt_lineage SET taint=1 WHERE job=? AND generation=?",
                   (job, m['lease_generation']))
        # Preserve an evidence row for a generation whose attempt never reported back.
        if not row(db, "SELECT 1 FROM attempts WHERE job=? AND generation=?",
                   (job, m['lease_generation'])):
            db.execute("INSERT INTO attempts(job,generation,status,started,ended) VALUES(?,?,?,?,?)",
                       (job, m['lease_generation'], 'quarantined', m['issued_at'], at))
    # A running attempt on the isolated generation can never become a delivery.
    db.execute("UPDATE attempts SET status='quarantined', ended=? WHERE job=? AND status='running'",
               (at, job))
    db.execute("DELETE FROM delivery_eligibility WHERE job=?", (job,))
    return record(db, 'secret_quarantine', job, 'quarantined',
                  {'job': job, 'slot': lease['slot'], 'generation': lease['generation'], 'reason': reason,
                   'temp_bytes': 'condemned_no_secret_material_stored'})


def standby_mount(db, job, generation, at, ttl=MOUNT_TTL_CAP):
    """Mount the current version on a standby generation after quarantine.

    The standby must be a different slot and the old lease must no longer be
    active, so two machines can never hold write capability simultaneously.
    """
    _ensure_tables(db)
    lease = row(db, "SELECT * FROM leases WHERE job=?", (job,))
    old_slot = row(db, "SELECT slot FROM secret_mounts WHERE job=? AND status='quarantined'"
                       " ORDER BY issued_at DESC LIMIT 1", (job,))
    if not lease or lease['status'] != 'active' or lease['generation'] != generation:
        return record(db, 'secret_mount', job, 'standby_blocked',
                      {'reason': 'lease_not_active', 'generation': generation})
    if old_slot and lease['slot'] == old_slot['slot']:
        return record(db, 'secret_mount', job, 'standby_blocked',
                      {'reason': 'same_slot_as_quarantined_mount', 'slot': lease['slot']})
    stale = row(db, "SELECT status FROM leases WHERE job=? AND generation<>? AND status='active'",
                (job, generation))
    if stale:
        return record(db, 'secret_mount', job, 'dual_mount_blocked',
                      {'reason': 'prior_generation_still_active', 'generation': generation})
    return issue_mount(db, job, generation, at, ttl)


# --------------------------------------------------------------------------- #
# Delivery eligibility and evidence chain
# --------------------------------------------------------------------------- #

def _cured(db, job, key_id, version):
    lineage = rows(db, "SELECT * FROM attempt_lineage WHERE job=? AND key_id=? AND version=?",
                   (job, key_id, version))
    cures = []
    for ln in lineage:
        lease = row(db, "SELECT slot FROM leases WHERE job=? AND generation=?", (job, ln['generation']))
        slot = lease['slot'] if lease else None
        att = row(db, "SELECT attestation_id FROM container_attestations WHERE slot=? AND job=? AND key_id=?"
                      " AND version=? AND verdict='unexposed'", (slot, job, key_id, version))
        rcpt = row(db, "SELECT receipt_id FROM kms_revocations WHERE key_id=? AND version=?"
                       " AND action IN ('reclaim','revoke') LIMIT 1", (key_id, version))
        if att and rcpt:
            cures.append({'generation': ln['generation'], 'attestation_id': att['attestation_id'],
                          'receipt_id': rcpt['receipt_id']})
        else:
            return False, None
    return True, cures


def assess_delivery(db, job, at, pending_generation=None):
    """Decide whether the job's current artifact may be delivered.

    Only the lineage of the delivery generation (the latest clean generation
    that already produced a successful attempt) is eligible; attempts condemned
    by quarantine stay in the chain as evidence but cannot block a clean
    recomputation once that recomputation has finished.
    """
    _ensure_tables(db)
    lineage = rows(db, "SELECT * FROM attempt_lineage WHERE job=? ORDER BY lineage_seq", (job,))
    if not lineage:
        return {'eligible': not bool(row(db, "SELECT 1 FROM secret_envelope_versions WHERE job=?", (job,))),
                'tainted_versions': [], 'evidence': {'reason': 'no_secret_access'}}
    job_row = row(db, "SELECT tenant FROM jobs WHERE id=?", (job,))
    tenant = job_row['tenant'] if job_row else None
    condemned = {q['lease_generation'] for q in rows(db,
        "SELECT DISTINCT lease_generation FROM secret_mounts WHERE job=? AND status='quarantined'", (job,))}
    clean_generations = sorted({x['generation'] for x in lineage if x['generation'] not in condemned})
    # Condemned generations retire only once a clean recomputation has been
    # confirmed; the attempt currently being settled is still in flight.
    confirmed_clean = [r['generation'] for r in rows(
        db, "SELECT generation FROM clean_recomputations WHERE job=? ORDER BY generation", (job,))]
    pending_row = next((x for x in lineage if x['generation'] == pending_generation), None)
    pending_is_safe = bool(
        pending_row and pending_row['generation'] not in condemned
        and pending_row['version'] not in _revoked_versions(db, pending_row['key_id'], tenant)
        and not pending_row['taint'])
    clean_retirement_ready = bool(confirmed_clean)
    delivery_generation = (confirmed_clean or
                           ([pending_generation] if pending_is_safe else clean_generations) or [None])[-1]
    tainted, cured_reads, reads = [], [], []
    for ln in lineage:
        revoked = ln['version'] in _revoked_versions(db, ln['key_id'], tenant)
        quarantined = ln['generation'] in condemned
        is_delivery = ln['generation'] == delivery_generation
        reads.append({'generation': ln['generation'], 'key_id': ln['key_id'], 'version': ln['version'],
                      'mount_id': ln['mount_id'], 'revoked': revoked, 'taint': ln['taint'],
                      'quarantined': quarantined, 'delivery_generation': is_delivery,
                      'recovery_id': ln['recovery_id']})
        if quarantined:
            # Condemned generations block delivery until a clean recomputation has
            # actually succeeded; afterwards they are retained as evidence only.
            if not clean_retirement_ready:
                tainted.append({'key_id': ln['key_id'], 'version': ln['version'],
                                'generation': ln['generation'], 'quarantined': True})
            continue
        if revoked:
            ok, cures = _cured(db, job, ln['key_id'], ln['version'])
            if ok:
                cured_reads.extend(cures)
            else:
                tainted.append({'key_id': ln['key_id'], 'version': ln['version'],
                                'generation': ln['generation']})
    # No clean recomputation has succeeded yet; the condemned reads still govern.
    if condemned and not clean_retirement_ready and not tainted:
        tainted.append({'reason': 'recomputation_pending', 'condemned_generations': sorted(condemned)})
    revocations = rows(db, "SELECT id,key_id,version,reason,status,at FROM secret_revocations"
                           " WHERE key_id IN (SELECT DISTINCT key_id FROM attempt_lineage WHERE job=?)", (job,))
    receipts = rows(db, "SELECT receipt_id,key_id,version,action,confirmed_at,duplicated FROM kms_revocations"
                        " WHERE key_id IN (SELECT DISTINCT key_id FROM attempt_lineage WHERE job=?)", (job,))
    evidence = {'reads': reads, 'revocations': revocations, 'kms_receipts': receipts,
                'cured_reads': cured_reads,
                'recovery_points': rows(db, "SELECT recovery_id,lease_generation,slot,key_id,version,point_digest,"
                                            "tainted,parent_recovery_id FROM recovery_points WHERE job=?", (job,))}
    eligible = not tainted
    import json
    db.execute("INSERT OR REPLACE INTO delivery_eligibility VALUES(?,?,?,?,?)",
               (job, int(eligible), json.dumps(tainted, sort_keys=True),
                json.dumps(evidence, sort_keys=True, ensure_ascii=False), at))
    return {'eligible': eligible, 'tainted_versions': tainted, 'evidence': evidence}


def confirm_clean_recomputation(db, job, generation, at, digest):
    """Mark a generation's safe recomputation as the delivery-grade result."""
    _ensure_tables(db)
    db.execute("INSERT OR REPLACE INTO clean_recomputations VALUES(?,?,?,?)",
               (job, generation, at, digest))
    db.execute("DELETE FROM delivery_eligibility WHERE job=?", (job,))
    return record(db, 'clean_recomputation', job, 'confirmed',
                  {'generation': generation, 'digest': digest, 'at': at})


def evidence_chain(db, job):
    """Replayable chain: revocation reasons, KMS confirmations, attempts, digests."""
    _ensure_tables(db)
    return {
        'job': job,
        'envelopes': rows(db, "SELECT key_id,version,sealed_digest,status,registered_at FROM"
                              " secret_envelope_versions WHERE job=? ORDER BY version", (job,)),
        'revocations': rows(db, "SELECT * FROM secret_revocations WHERE key_id IN"
                                " (SELECT DISTINCT key_id FROM secret_envelope_versions WHERE job=?)", (job,)),
        'mounts': rows(db, "SELECT mount_id,key_id,version,lease_generation,slot,issued_at,expires_at,status,"
                           "revocation_id,recovery_id FROM secret_mounts WHERE job=? ORDER BY issued_at", (job,)),
        'kms_receipts': rows(db, "SELECT * FROM kms_revocations WHERE key_id IN"
                                 " (SELECT DISTINCT key_id FROM secret_envelope_versions WHERE job=?)", (job,)),
        'attestations': rows(db, "SELECT * FROM container_attestations WHERE job=?", (job,)),
        'rotation_intents': rows(db, "SELECT * FROM rotation_intents WHERE job=?", (job,)),
        'recovery_points': rows(db, "SELECT * FROM recovery_points WHERE job=? ORDER BY created_at", (job,)),
        'attempts': rows(db, "SELECT * FROM attempts WHERE job=? ORDER BY generation", (job,)),
        'lineage': rows(db, "SELECT * FROM attempt_lineage WHERE job=? ORDER BY generation", (job,)),
        'artifact': row(db, "SELECT * FROM artifacts WHERE job=?", (job,)),
        'eligibility_stored': row(db, "SELECT * FROM delivery_eligibility WHERE job=?", (job,)),
        'eligibility': assess_delivery(db, job, 0),
        'clean_recomputations': rows(db, "SELECT * FROM clean_recomputations WHERE job=?", (job,)),
    }


# --------------------------------------------------------------------------- #
# Hooks called by the existing jobs / slots subsystems
# --------------------------------------------------------------------------- #

def on_job_cancelled(db, job, at):
    _ensure_tables(db)
    for m in rows(db, "SELECT mount_id FROM secret_mounts WHERE job=? AND status='active'", (job,)):
        db.execute("UPDATE secret_mounts SET status='cancelled' WHERE mount_id=?", (m['mount_id'],))
    for intent in rows(db, "SELECT intent_id FROM rotation_intents WHERE job=? AND status!='closed'", (job,)):
        db.execute("UPDATE rotation_intents SET status='closed', decision='cancelled' WHERE intent_id=?",
                   (intent['intent_id'],))


def on_slot_isolated(db, slot, at):
    _ensure_tables(db)
    # Both ordinary active mounts and mounts frozen by revocation become lease
    # isolation evidence once the slot is declared unhealthy.
    db.execute("UPDATE secret_mounts SET status='isolated' WHERE slot=? AND status IN"
               " ('active','revoked_pending')", (slot,))
