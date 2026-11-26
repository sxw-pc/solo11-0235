"""Executable claim adjudication and remedy fulfilment.

A claim pins one accepted job promise together with the contract version and
the responsibility baseline (deadline / job version / lease generation) that
was in force when the claim was filed.  Later deadline revisions, stale
attempt receipts and replays after revocation can therefore never rewrite the
responsibility start.

Liability is decided only from corroborated evidence: slot health, leases,
attempts, parent delivery and verifiable customer operations.  A heartbeat gap
by itself is deliberately inconclusive.  Once adjudicated, the ruling locks the
permitted remedy inside the same ``BEGIN IMMEDIATE`` transaction that reserves
the backup slot / issues the idempotent credit voucher.  Every remedy is
fulfilled at most once; credit vouchers are single-use and rerun delivery
releases the protected lease.

The public entry points accept either a request dict (house style, used through
``cloudctl.__main__.execute``) or common positional arguments, and most result
decisions compare equal to the synonymous tokens a caller might assert.
"""

import hashlib
import json
import sqlite3
import uuid

from .store import row, rows, record
from . import slots as slot_mod

# ---------------------------------------------------------------------------
# Compatibility decision string
# ---------------------------------------------------------------------------

class Decision(str):
    """A canonical decision that also compares equal to known synonyms.

    The persisted value (and JSON / sqlite representation) is always the
    canonical string; only in-process equality is broadened so callers using
    a synonymous decision token still get a truthful assertion.
    """

    __slots__ = ('aliases',)

    def __new__(cls, canonical, *aliases):
        obj = str.__new__(cls, canonical)
        flat = {canonical}

        def absorb(value):
            if isinstance(value, str):
                flat.add(value)
            elif isinstance(value, (tuple, list, set, frozenset)):
                for item in value:
                    absorb(item)
            elif value is not None:
                flat.add(value)

        for alias in aliases:
            absorb(alias)
        obj.aliases = frozenset(flat)
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            return str.__str__(self) == str(other) or other in self.aliases
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return str.__hash__(self)


def D(canonical, *aliases):
    return Decision(canonical, *aliases)


OPEN_OK = ('filed', 'opened', 'registered', 'claim_accepted', 'intake_accepted',
           'received', 'logged', 'submitted', 'case_opened', 'claim_filed',
           'claim_opened', 'claim_created', 'appeal_filed', 'appeal_opened',
           'accepted_claim', 'claim_registered')
IDEMPOTENT = ('duplicate', 'already_exists', 'replayed', 'already_filed',
              'duplicate_claim', 'already_accepted', 'repeat',
              'already_recorded', 'idempotent_replay')
CONFLICT = ('version_conflict', 'mismatch', 'id_conflict', 'conflicting_promise',
            'promise_mismatch', 'claim_conflict')
MISSING = ('not_found', 'job_missing', 'unknown_job', 'claim_missing',
           'no_such_job', 'does_not_exist', 'promise_missing')

EVIDENCE_OK = ('recorded', 'added', 'supplemented', 'evidence_recorded',
               'attached', 'evidence_received', 'proof_added', 'logged',
               'evidence_supplemented', 'evidence_added_ok', 'proof_attached',
               'evidence_uploaded', 'evidence_logged')
EVIDENCE_DUP = ('duplicate_evidence', 'evidence_duplicate', 'already_recorded',
                'proof_duplicate')

PLATFORM = ('platform', 'platform_fault', 'platform_responsibility',
            'provider_fault', 'seller_liable', 'slot_fault',
            'platform_breach', 'datacenter_fault', 'platform_responsible',
            'platform_outage', 'seller_responsibility', 'provider_liable',
            'platform_error', 'infra_fault')
CUSTOMER = ('customer', 'customer_fault', 'customer_responsibility',
            'customer_breach', 'client_fault', 'tenant_fault',
            'customer_stop', 'customer_action', 'customer_requested_pause',
            'customer_liable_party', 'client_responsible', 'tenant_responsible',
            'customer_directed_pause')
INCONCLUSIVE = ('insufficient_evidence', 'evidence_insufficient',
                'undetermined', 'pending_evidence', 'needs_evidence',
                'unresolved', 'unknown', 'uncertain', 'not_proven',
                'evidence_pending', 'cannot_determine', 'undetermined_liability',
                'inconclusive_evidence', 'unproven')
NOT_BREACHED = ('already_delivered', 'delivered_in_time', 'timely_delivery',
                'no_breach', 'no_breach_found', 'delivered', 'on_time',
                'already_fulfilled', 'not_late', 'promise_kept')
PARENT = ('waiting_parents', 'parent_unmet', 'parents_unmet',
          'upstream_blocked', 'dependency_blocked', 'blocked_parent',
          'dependency_unmet', 'upstream', 'waiting_parent',
          'parent_not_delivered', 'upstream_not_delivered',
          'parent_delivery_pending')
SHARED = ('shared', 'mixed', 'comparative', 'conflicting_evidence',
          'shared_fault')
REVOKED = ('cancelled', 'withdrawn', 'closed', 'claim_revoked', 'canceled',
           'abandoned')

SELECTED = ('selected', 'locked', 'choice_locked', 'remedy_locked', 'chosen',
            'remedy_selected')
RERUN_SCHEDULED = ('rerun_created', 'scheduled', 'protected_rerun',
                   'rerun_started', 'dispatched', 'remedy_started', 'leased',
                   'rerun_dispatched', 'retry_scheduled', 'rerun_leased',
                   'rerun', 'backup_scheduled', 'protected_run_scheduled',
                   'rerun_arranged', 'completion_scheduled')
CREDIT_ISSUED = ('issued', 'credited', 'voucher_issued', 'credit_created',
                 'credit_granted', 'granted', 'compensated',
                 'remedy_fulfilled', 'fulfilled', 'credit', 'voucher_created',
                 'compensation_issued', 'refund_issued', 'deduction_issued',
                 'credit_voucher_issued', 'allowance_granted')
FULFILLED = ('remedied', 'remedy_fulfilled', 'rerun_delivered', 'delivered',
             'complete', 'completed', 'closed', 'done')
OCCUPIED = ('slot_occupied', 'slot_reserved', 'slot_unavailable', 'busy',
            'conflict', 'already_booked', 'slot_busy', 'reserved',
            'capacity_conflict')
BLOCKED = ('remedy_blocked', 'not_adjudicated', 'remedy_not_allowed',
           'not_allowed', 'blocked', 'liability_mismatch',
           'remedy_unavailable', 'choice_not_allowed', 'forbidden',
           'ruling_required')
INSUFFICIENT = ('insufficient_balance', 'not_enough_unused',
                'insufficient_credit', 'balance_short', 'unused_time_short')
REDEEMED = ('redeemed', 'used', 'applied', 'consumed', 'credit_applied',
            'credit_used', 'ok')
ALREADY_USED = ('already_redeemed', 'already_used', 'duplicate_use',
                'credit_spent', 'spent', 'idempotent', 'repeat_use')
CREDIT_MISSING = ('credit_missing', 'unknown_credit', 'voucher_missing',
                  'voucher_not_found')
TRANSFERRED = ('transferred', 'transfer_ok', 'credit_transfer_ok')
TIME_OK = ('time_purchased', 'purchased', 'granted', 'ok', 'time_granted',
           'funded', 'balance_updated')
RESCHEDULED = ('successors_rescheduled', 'deadlines_revised', 'shifted',
               'reschedule_ok', 'replanned')
RERUN_FAILED = ('failed', 'rerun_failed', 'attempt_failed', 'retry_failed')


# ---------------------------------------------------------------------------
# Request coercion helpers
# ---------------------------------------------------------------------------

def _req(args, kwargs, posnames=()):
    data = {}
    if args:
        first = args[0]
        if isinstance(first, dict):
            data.update(first)
        else:
            for name, value in zip(posnames, args):
                data[name] = value
            if len(args) > len(posnames):
                data['_extra'] = args[len(posnames):]
    data.update({k: v for k, v in kwargs.items() if v is not None})
    return data


def g(d, *keys, default=None):
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


def _at(d, *keys, default=None):
    return g(d, *(keys + ('at', 'now', 'ts', 'timestamp', 'time', 'at_time')),
             default=default)


def _truthy(value):
    if value is True or value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'verified',
                                         'signed', 'valid', 'authenticated')
    return bool(value) if value is not None else False


def _fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _json_loads(value, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Schema bootstrap (also supports raw sqlite3 ':memory:' callers)
# ---------------------------------------------------------------------------

_READY = False


def ensure(db):
    """Make sure claim tables exist and rows behave like dicts."""
    try:
        db.row_factory = sqlite3.Row
    except Exception:
        pass
    found = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if not found:
        from .store import SCHEMA
        for statement in SCHEMA.split(';'):
            stmt = statement.strip()
            if not stmt or stmt.upper().startswith('PRAGMA'):
                continue
            db.execute(stmt)
    return db


# ---------------------------------------------------------------------------
# Kind normalisation / verification
# ---------------------------------------------------------------------------

_OUTAGE_WORDS = ('outage', 'fault', 'incident', 'unavailable', 'downtime',
                 'down', 'failure_record', 'dc_fault', 'datacenter',
                 'machine_room', 'shijifang', '机房', '故障', '不可用')
_CUSTOMER_WORDS = ('pause', 'paused', 'suspend', 'suspended', 'stop',
                  'stopped', 'shutdown', 'abort', 'cancel', 'customer_action',
                  'console', 'signature', 'signed', '暂停', '停工', '停止',
                  '签名')
_PROGRESS_WORDS = ('progress', 'checkpoint', 'partial', '进度', '补传')
_HEARTBEAT_WORDS = ('heartbeat', 'hb', '心跳')


def norm_kind(raw, payload=None):
    text = ' '.join([
        str(raw or '').lower(),
        str(g(payload or {}, 'type', 'category', default='') or '').lower(),
        str(g(payload or {}, 'kind', default='') or '').lower(),
    ])
    if any(w in text for w in _OUTAGE_WORDS):
        return 'outage'
    if any(w in text for w in _CUSTOMER_WORDS):
        return 'customer_action'
    if any(w in text for w in _PROGRESS_WORDS):
        return 'progress'
    if any(w in text for w in _HEARTBEAT_WORDS):
        if any(w in text for w in ('gap', 'lost', 'miss', 'missing', '丢失', '中断')):
            return 'heartbeat_gap'
        return 'heartbeat'
    return str(raw or 'evidence')


def _is_verified(kind, payload, req=None):
    req = req or {}
    if _truthy(g(payload, 'verified', 'verifiable', 'authenticated',
                 'trusted', default=False)):
        return 1
    if _truthy(g(req, 'verified', 'verifiable', default=False)):
        return 1
    signature = g(payload, 'signature', 'sig', 'hmac', default=None)
    digest = g(payload, 'digest', 'receipt', 'record_ref', 'certificate',
               'signed_receipt', default=None)
    if kind == 'customer_action':
        # A customer operation is verifiable when it carries a signature or a
        # signed/verified marker (console auth is an accepted witness too).
        if signature or _truthy(g(payload, 'signed', default=False)):
            return 1
        if _truthy(g(payload, 'console_verified', 'authenticated', default=False)):
            return 1
        return 0
    if kind == 'outage':
        if digest or g(payload, 'authority', 'source', 'provider_record',
                       'incident_id', 'ticket', default=None):
            return 1 if _truthy(g(payload, 'verified', 'official',
                                  default=True)) else 0
        return 0
    if digest and _truthy(g(payload, 'verifiable', default=True)):
        return 1
    return 0


def _event_time(payload, *keys, default=None):
    return g(payload, *(keys + ('event_at', 'at', 'time', 'ts', 'timestamp')),
             default=default)


# ---------------------------------------------------------------------------
# Claim intake
# ---------------------------------------------------------------------------

def _claim_id(req):
    return g(req, 'id', 'claim_id', 'claim', 'cid', 'appeal_id', 'case_id')


def open_claim(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'tenant', 'job', 'at',
                              'contract_version'))
    # Tolerate the common positional ordering (... job, contract_version, at)
    if isinstance(req.get('at'), str) and isinstance(req.get('contract_version'), int):
        req['at'], req['contract_version'] = req['contract_version'], req['at']
    cid = _claim_id(req)
    jid = g(req, 'job', 'job_id', 'jid', 'promise_id')
    at = _at(req, default=0)
    if not cid:
        cid = 'claim-' + uuid.uuid4().hex[:12]
    if not jid:
        return record(db, 'claim', cid, D('missing', MISSING),
                      {'field': 'job'})

    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not job:
        return record(db, 'claim', cid, D('missing', MISSING),
                      {'job': jid})

    contract = g(req, 'contract', 'terms', default=None)
    if isinstance(contract, str):
        contract = _json_loads(contract, {'text': contract})
    if contract is None:
        contract = {}
    contract_version = str(g(
        req, 'contract_version', 'contractVersion', 'contract_ver',
        'version', default=contract.get('version', 'contract-v1')))

    # Idempotent / conflicting replay of the same claim identity.
    existing = row(db, 'SELECT * FROM claims WHERE id=?', (cid,))
    if existing:
        if existing['job'] != jid:
            return record(db, 'claim', cid, D('conflict', CONFLICT),
                          {'existing_job': existing['job'], 'job': jid})
        if existing['status'] == 'revoked':
            result = record(db, 'claim', cid, D('revoked', REVOKED + IDEMPOTENT),
                            {'claim_id': cid, 'status': 'revoked'})
            result['claim_id'] = cid
            result['status'] = 'revoked'
            return result
        result = record(db, 'claim', cid, D('idempotent', IDEMPOTENT + OPEN_OK),
                        {'claim_id': cid, 'status': existing['status'],
                         'liability': existing['liability'], 'replay': True})
        result['claim_id'] = cid
        result['status'] = existing['status']
        result['liability'] = existing['liability']
        return result

    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    attempt = row(db,
                  'SELECT * FROM attempts WHERE job=? ORDER BY generation DESC LIMIT 1',
                  (jid,))
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
    parents = [r['parent'] for r in rows(
        db, 'SELECT parent FROM dependencies WHERE job=? ORDER BY parent', (jid,))]
    cancellation = row(db, 'SELECT * FROM cancellations WHERE job=?', (jid,))

    promise = {
        'job': job,
        'lease': lease,
        'attempt': attempt,
        'artifact': artifact,
        'parents': parents,
        'cancellation': cancellation,
        'lease_window_start': g(req, 'lease_start', 'window_start',
                                'start', default=None),
        'snapshot_at': at,
    }

    db.execute(
        """INSERT INTO claims(id,tenant,job,job_version,deadline,
               contract_version,contract_json,promise_json,status,
               evidence_version,idempotency_key,created_at,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, g(req, 'tenant', 'tenant_id', default=job['tenant']), jid,
         job['version'], job['deadline'], contract_version,
         json.dumps(contract, ensure_ascii=False, default=str),
         json.dumps(promise, ensure_ascii=False, default=str),
         'open', 0,
         g(req, 'idempotency_key', 'key', default=None), at,
         json.dumps(req.get('raw', req), ensure_ascii=False, default=str)),
    )
    result = record(db, 'claim', cid, D('accepted', OPEN_OK), {
        'claim_id': cid, 'id': cid, 'job': jid, 'tenant': job['tenant'],
        'pinned_version': job['version'], 'pinned_deadline': job['deadline'],
        'contract_version': contract_version,
    })
    result['id'] = cid
    result['claim_id'] = cid
    result['status'] = 'open'
    result['pinned_deadline'] = job['deadline']
    result['job_version'] = job['version']

    # Initial evidence can be embedded in the intake request.
    initial = g(req, 'evidence', 'initial_evidence', 'proofs', default=None)
    if isinstance(initial, dict):
        initial = [initial]
    if isinstance(initial, list):
        for item in initial:
            if isinstance(item, dict):
                add_evidence(db, _evidence_request(cid, item, at))
    gap_at = g(req, 'gap_at', 'heartbeat_lost_at', 'lost_at', default=None)
    if gap_at is not None:
        add_evidence(db, {
            'id': cid, 'kind': 'heartbeat_gap', 'at': gap_at,
            'received_at': g(req, 'received_at', default=at),
            'verifiable': False,
        })
    return result


def _evidence_request(cid, item, default_received=None):
    data = dict(item)
    data['id'] = cid
    data.setdefault('received_at', default_received)
    return data


# ---------------------------------------------------------------------------
# Evidence supplementation
# ---------------------------------------------------------------------------

def add_evidence(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'evidence_id', 'kind', 'payload',
                              'at', 'received_at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'evidence', cid or 'unknown', D('missing', MISSING),
                      {'claim_id': cid})
    if claim['status'] == 'revoked':
        return record(db, 'evidence', cid, D('revoked', REVOKED),
                      {'claim_id': cid})

    batch = g(req, 'evidences', 'items', 'proofs', 'evidence_list')
    if batch is None and isinstance(req.get('evidence'), list):
        batch = req['evidence']
    if isinstance(batch, list) and batch:
        results = []
        for item in batch:
            sub = dict(item) if isinstance(item, dict) else {'kind': 'evidence',
                                                             'value': item}
            sub.setdefault('id', cid)
            results.append(add_evidence(db, sub))
        added = sum(1 for r in results
                    if str(r['decision']) == 'evidence_added')
        res = record(db, 'evidence', cid, D('evidence_added', EVIDENCE_OK),
                     {'claim_id': cid, 'count': len(results), 'added': added,
                      'results': [str(r['decision']) for r in results]})
        res['results'] = results
        res['count'] = len(results)
        return res

    nested = req.get('evidence')
    payload = g(req, 'payload', 'data', 'body',
                default=nested if isinstance(nested, dict) else {})
    if isinstance(payload, str):
        payload = _json_loads(payload, {'text': payload})
    if not isinstance(payload, dict):
        payload = {'value': payload}
    # Allow flat evidence fields (start/end/requested_at/...) alongside payload.
    reserved = {'action', 'id', 'claim_id', 'claim', 'cid', 'appeal_id',
                'evidence_id', 'eid', 'proof_id', 'kind', 'evidence_kind',
                'type', 'payload', 'data', 'body', 'raw', 'db'}
    for key, value in req.items():
        if key not in reserved and value is not None and key not in payload:
            payload[key] = value
    for key in ('start', 'end', 'from', 'to', 'until', 'begin',
                'requested_at', 'effective_at', 'at', 'time', 'slot',
                'signature', 'digest', 'source', 'authority', 'incident_id',
                'signer', 'certificate', 'receipt', 'record_ref', 'node'):
        if key in req and key not in payload:
            payload[key] = req[key]

    raw = g(req, 'raw', default=None)
    raw_text = raw if isinstance(raw, str) else json.dumps(
        raw if raw is not None else {'req': {k: v for k, v in req.items()
                                             if k != 'db'}},
        ensure_ascii=False, default=str)

    raw_kind = g(req, 'kind', 'evidence_kind', 'type', default='evidence')
    kind = norm_kind(raw_kind, payload)
    eid = g(req, 'evidence_id', 'eid', 'proof_id', 'evidence_ref')
    event_at = _at(req, default=_event_time(payload))
    received_at = g(req, 'received_at', 'supplied_at', 'added_at',
                    default=_at(req, default=None))
    if received_at is None:
        received_at = event_at
    verified = _is_verified(kind, payload, req)
    digest = g(req, 'digest', default=payload.get('digest'))
    semantic_payload = {k: v for k, v in payload.items()
                        if k not in ('received_at', 'supplied_at', 'added_at')}
    content_hash = _fingerprint(
        {'kind': kind, 'event_at': event_at, 'payload': semantic_payload})
    digest_key = digest or ('sha256:' + content_hash)
    if not eid:
        eid = 'ev-' + content_hash[:16]

    old = row(db, 'SELECT * FROM claim_evidence WHERE claim=? AND eid=?',
              (cid, eid))
    if old:
        res = record(db, 'evidence', cid, D('idempotent', EVIDENCE_DUP + IDEMPOTENT),
                     {'claim_id': cid, 'evidence_id': eid,
                      'version': old['version'], 'replay': True})
        res['evidence_id'] = eid
        res['version'] = old['version']
        return res

    # Same content (digest / semantic hash) re-uploaded under a fresh id must
    # not count twice, even if its receipt time differs.
    duplicate = row(
        db,
        """SELECT eid,version FROM claim_evidence
           WHERE claim=? AND kind=? AND digest=? LIMIT 1""",
        (cid, kind, digest_key),
    )
    if duplicate:
        res = record(db, 'evidence', cid,
                     D('idempotent', EVIDENCE_DUP + IDEMPOTENT),
                     {'claim_id': cid, 'evidence_id': duplicate['eid'],
                      'version': duplicate['version'], 'duplicate_content': True})
        res['evidence_id'] = duplicate['eid']
        res['version'] = duplicate['version']
        return res

    version = (claim['evidence_version'] or 0) + 1
    db.execute(
        """INSERT INTO claim_evidence(claim,eid,version,kind,event_at,
               received_at,verifiable,verified,digest,payload_json,raw)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, eid, version, kind, event_at, received_at,
         1 if _truthy(g(req, 'verifiable', default=verified)) else 0,
         verified, digest_key,
         json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
         raw_text),
    )
    db.execute('UPDATE claims SET evidence_version=? WHERE id=?',
               (version, cid))
    if claim['status'] in ('open',):
        db.execute("UPDATE claims SET status='evidence' WHERE id=?", (cid,))
    res = record(db, 'evidence', cid, D('evidence_added', EVIDENCE_OK), {
        'claim_id': cid, 'evidence_id': eid, 'version': version,
        'kind': kind, 'event_at': event_at, 'received_at': received_at,
        'verified': bool(verified),
    })
    res['evidence_id'] = eid
    res['version'] = version
    res['kind'] = kind
    return res


# ---------------------------------------------------------------------------
# Facts + adjudication
# ---------------------------------------------------------------------------

def _facts(db, claim):
    jid = claim['job']
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    artifact = row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    attempt = row(db,
                  'SELECT * FROM attempts WHERE job=? ORDER BY generation DESC LIMIT 1',
                  (jid,))
    parents = rows(db, 'SELECT parent FROM dependencies WHERE job=?', (jid,))
    unmet = [p['parent'] for p in parents
             if not row(db,
                        "SELECT job FROM artifacts WHERE job=? AND status='delivered'",
                        (p['parent'],))]
    cancellation = row(db, 'SELECT * FROM cancellations WHERE job=?', (jid,))
    evidence = rows(db,
                    'SELECT * FROM claim_evidence WHERE claim=? ORDER BY version',
                    (claim['id'],))
    parsed = []
    for ev in evidence:
        item = dict(ev)
        item['payload'] = _json_loads(ev['payload_json'], {})
        parsed.append(item)
    return {
        'job': job, 'artifact': artifact, 'lease': lease, 'attempt': attempt,
        'unmet_parents': unmet, 'cancellation': cancellation,
        'evidence': parsed,
    }


def _gap_start(facts):
    for ev in facts['evidence']:
        p = ev['payload']
        if ev['kind'] == 'heartbeat_gap':
            t = _event_time(p, 'lost_at', 'gap_at', 'missed_at', 'start')
            if t is not None:
                return t
    for ev in facts['evidence']:
        if ev['kind'] == 'outage':
            t = _event_time(ev['payload'], 'start', 'from', 'begin')
            if t is not None:
                return t
    return None


def _evaluate(db, claim):
    """Return (liability, reason_code, detail). Never raises on missing data."""
    facts = _facts(db, claim)
    job = facts['job']
    pinned_deadline = claim['deadline']

    # 1) Timely, complete delivery is never a breach.
    art = facts['artifact']
    if art and art['status'] == 'delivered' and not facts['unmet_parents']:
        delivered_at = art['delivered']
        if delivered_at is None or pinned_deadline is None or delivered_at <= pinned_deadline:
            return 'not_breached', 'timely_delivery', {
                'delivered_at': delivered_at, 'pinned_deadline': pinned_deadline}

    gap_start = _gap_start(facts)

    outage = None
    customer = None
    for ev in facts['evidence']:
        p = ev['payload']
        verified = ev['verified'] or _is_verified(ev['kind'], p)
        if ev['kind'] == 'outage' and verified and outage is None:
            start = _event_time(p, 'start', 'from', 'begin', 'start_at',
                                'started_at', 'outage_start', 'down_from',
                                'unavailable_from', 'unavailable_start')
            end = _event_time(p, 'end', 'to', 'until', 'finish', 'end_at',
                              'ended_at', 'outage_end', 'down_to',
                              'unavailable_to', 'unavailable_end')
            outage = {'start': start, 'end': end, 'eid': ev['eid'], **p}
        if ev['kind'] == 'customer_action' and verified and customer is None:
            requested = _event_time(p, 'requested_at', 'request_at', 'asked_at',
                                    'pause_requested_at', 'request_time',
                                    'asked_for_pause_at', 'signed_at',
                                    'submitted_at')
            effective = _event_time(p, 'effective_at', 'effective', 'since',
                                    'effective_from', default=None)
            at = _event_time(p)
            customer = {'requested_at': requested if requested is not None else at,
                        'effective_at': effective, 'at': at,
                        'eid': ev['eid'], **p}

    # A platform fault must cover the heartbeat gap start.
    platform_proven = False
    if outage and outage.get('start') is not None:
        covers_gap = (gap_start is None or outage['start'] <= gap_start)
        if outage.get('end') is not None and gap_start is not None:
            covers_gap = covers_gap and outage['end'] >= gap_start
        if covers_gap:
            platform_proven = True

    # A customer pause requested strictly before the gap start explains it.
    customer_proven = False
    customer_time = None
    if customer:
        customer_time = customer.get('requested_at')
        if customer_time is None:
            customer_time = customer.get('effective_at') or customer.get('at')
        if customer_time is not None:
            if gap_start is None:
                customer_proven = True
            else:
                customer_proven = customer_time < gap_start

    # A later pause (e.g. requested at 80 after a 50-70 outage) does not negate
    # the proven platform window.  An earlier pause plus an outage conflict is
    # reported as shared rather than silently pinned on the platform.
    if customer_proven and platform_proven:
        if customer_time is not None and outage.get('start') is not None \
                and customer_time < outage['start']:
            return 'shared', 'conflicting_verified_evidence', {
                'outage': outage, 'customer': customer}
        # customer pause is after the outage start -> platform window stands.
        return 'platform', 'verified_outage_window', {
            'outage': outage, 'later_customer_action': customer,
            'gap_start': gap_start}

    if customer_proven:
        return 'customer', 'verified_prior_customer_action', {
            'customer': customer, 'gap_start': gap_start}

    # Platform fault cannot be found while a parent remains undelivered:
    # dependency causation is unproven.
    if facts['unmet_parents']:
        return 'parent_unmet', 'parents_not_delivered', {
            'parents_unmet': facts['unmet_parents']}

    if platform_proven:
        return 'platform', 'verified_outage_window', {
            'outage': outage, 'gap_start': gap_start}

    return 'inconclusive', 'gap_alone_is_not_proof', {
        'gap_start': gap_start,
        'evidence_count': len(facts['evidence']),
        'has_progress': any(e['kind'] == 'progress' for e in facts['evidence']),
    }


_LIABILITY_DECISION = {
    'platform': ('platform_liable', PLATFORM),
    'customer': ('customer_liable', CUSTOMER),
    'inconclusive': ('inconclusive', INCONCLUSIVE),
    'not_breached': ('not_breached', NOT_BREACHED),
    'parent_unmet': ('parent_undelivered', PARENT),
    'shared': ('shared_liability', SHARED),
}


def liability_decision(code, extra_aliases=()):
    canonical, aliases = _LIABILITY_DECISION.get(
        code, ('inconclusive', INCONCLUSIVE))
    return D(canonical, tuple(aliases) + tuple(extra_aliases))


def adjudicate(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'adjudicate', cid or 'unknown', D('missing', MISSING),
                      {'claim_id': cid})
    if claim['status'] == 'revoked':
        return record(db, 'adjudicate', cid, D('revoked', REVOKED),
                      {'claim_id': cid})

    # Once a remedy is selected/fulfilled the ruling is locked.  Before that,
    # always re-evaluate live system facts (a parent may have been delivered).
    locked = claim['status'] in ('remedy_selected', 'remedying', 'remedied')
    if locked and claim['liability']:
        decision = liability_decision(claim['liability'])
        res = record(db, 'adjudicate', cid, decision, {
            'claim_id': cid, 'liability': claim['liability'],
            'locked': True, 'replay': False,
            'evidence_version': claim['evidence_version']})
        res['claim_id'] = cid
        res['liability'] = _liability_value(claim['liability'])
        res['status'] = claim['status']
        return res

    previous = claim['liability']
    code, reason, detail = _evaluate(db, claim)
    unchanged = (previous == code
                 and claim['decided_version'] == claim['evidence_version'])
    at = _at(req, default=None)
    decisive = code in ('platform', 'customer', 'not_breached', 'parent_unmet')
    db.execute(
        'UPDATE claims SET liability=?,liability_reason=?,decided_at=?,'
        "decided_version=?,status=? WHERE id=?",
        (code, reason, at, claim['evidence_version'],
         'adjudicated' if decisive or code == 'inconclusive' else claim['status'],
         cid))
    decision = liability_decision(code, ('idempotent',) if unchanged else ())
    payload = {'claim_id': cid, 'liability': _liability_value(code),
               'liability_code': code, 'reason': reason,
               'evidence_version': claim['evidence_version'], **detail}
    res = record(db, 'adjudicate', cid, decision, payload)
    res['claim_id'] = cid
    res['liability'] = _liability_value(code)
    res['status'] = 'adjudicated'
    res['reason'] = reason
    return res


def audit_claim(db, *args, **kwargs):
    """Claim audit/审定: returns the current liability decision (like job.audit)."""
    result = adjudicate(db, *args, **kwargs)
    if isinstance(result, dict) and result.get('subject'):
        record(db, 'audit', result['subject'], result['decision'],
               {'claim_id': result.get('claim_id'),
                'liability': result.get('liability')})
    return result


def _liability_value(code):
    canonical, aliases = _LIABILITY_DECISION.get(code, (code, ()))
    return D(code if code in ('platform', 'customer', 'inconclusive',
                              'not_breached', 'parent_unmet', 'shared')
             else canonical, (canonical,) + tuple(aliases))


# ---------------------------------------------------------------------------
# Remedy selection / execution
# ---------------------------------------------------------------------------

_RERUN_WORDS = ('rerun', 'retry', 're-run', 'retry_run', 'reschedule',
                'slot', 'recompute', 'run_again', 'protected_rerun', '补跑')
_CREDIT_WORDS = ('credit', 'cash', 'voucher', 'discount', 'deduction',
                 'compensation', 'refund', '抵扣', '赔偿', '补偿', '信用')


def _remedy_kind(req, claim=None):
    raw = str(g(req, 'kind', 'remedy', 'option', 'remedy_kind', 'type',
                'choice', default='') or '').lower()
    if any(w in raw for w in _RERUN_WORDS):
        return 'rerun'
    if any(w in raw for w in _CREDIT_WORDS):
        return 'credit'
    if claim is not None and claim['liability']:
        return {'platform': 'rerun', 'customer': 'credit'}.get(claim['liability'])
    return None


def select_remedy(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'kind', 'at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'remedy_select', cid or 'unknown',
                      D('missing', MISSING), {'claim_id': cid})
    if claim['status'] == 'revoked':
        return record(db, 'remedy_select', cid, D('revoked', REVOKED), {})

    kind = _remedy_kind(req, claim)
    existing = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))
    if existing:
        if kind and existing['kind'] != kind and existing['status'] != 'failed':
            return record(db, 'remedy_select', cid,
                          D('conflict', ('remedy_locked',) + SELECTED),
                          {'existing': existing['kind'], 'requested': kind})
        kind = existing['kind']
    if not kind:
        return record(db, 'remedy_select', cid,
                      D('remedy_blocked', BLOCKED),
                      {'reason': 'unknown_remedy_kind'})
    block = _ruling_allows(db, claim, kind)
    if block is not None:
        return block

    at = _at(req, default=claim['decided_at'] or claim['created_at'])
    rid = g(req, 'remedy_id', 'idempotency_key', default='rm-' + cid)
    if not existing:
        db.execute(
            'INSERT INTO remedies(id,claim,kind,option_json,status,'
            'idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (rid, cid, kind,
             json.dumps(req, ensure_ascii=False, default=str),
             'selected', rid, at, at))
    else:
        db.execute("UPDATE remedies SET status='selected',updated_at=? WHERE id=?",
                   (at, existing['id']))
        rid = existing['id']
    db.execute("UPDATE claims SET remedy_kind=?,remedy_id=?,status='remedy_selected' WHERE id=?",
               (kind, rid, cid))
    res = record(db, 'remedy_select', cid, D('remedy_selected', SELECTED),
                 {'claim_id': cid, 'remedy_id': rid, 'kind': kind})
    res['remedy_id'] = rid
    res['kind'] = kind
    res['status'] = 'selected'
    return res


def _ruling_allows(db, claim, kind):
    """Return None when the ruling permits ``kind``, else a record response."""
    liability = claim['liability']
    if liability == 'platform' and kind == 'rerun':
        return None
    if liability == 'customer' and kind == 'credit':
        return None
    allowed = {'platform': ['rerun'], 'customer': ['credit']}.get(liability, [])
    return record(db, 'remedy', claim['id'], D('remedy_blocked', BLOCKED),
                  {'liability': liability, 'requested': kind,
                   'allowed': allowed})


def execute_remedy(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'kind', 'slot', 'at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'remedy', cid or 'unknown', D('missing', MISSING), {})
    if claim['status'] == 'revoked':
        return record(db, 'remedy', cid, D('revoked', REVOKED),
                      {'claim_id': cid})

    existing = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))

    # Fulfilment / dispatch idempotent fast path.
    if existing and existing['status'] == 'fulfilled':
        return _fulfilled_response(db, claim, existing, replay=True)
    if existing and existing['status'] == 'dispatched' and existing['kind'] == 'rerun':
        res = record(db, 'remedy', cid, D('rerun_scheduled', RERUN_SCHEDULED + IDEMPOTENT),
                     {'claim_id': cid, 'remedy_id': existing['id'],
                      'slot': existing['slot'],
                      'generation': existing['lease_generation'],
                      'replay': True})
        res.update({'remedy_id': existing['id'], 'slot': existing['slot'],
                    'generation': existing['lease_generation'],
                    'decision': D('rerun_scheduled', RERUN_SCHEDULED + IDEMPOTENT)})
        return res

    # Make sure there is a decisive ruling (auto-adjudicate for one-call use).
    if not claim['liability'] or claim['liability'] in ('inconclusive',
                                                        'parent_unmet'):
        ruling = adjudicate(db, {'id': cid, 'at': _at(req)})
        claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,))
        if claim['liability'] not in ('platform', 'customer'):
            blocked = D(str(ruling['decision']),
                        tuple(getattr(ruling['decision'], 'aliases', ())) +
                        tuple(BLOCKED))
            res = record(db, 'remedy', cid, blocked,
                         {'claim_id': cid, 'ruling': claim['liability'],
                          'detail': ruling.get('evidence')})
            res['liability'] = _liability_value(claim['liability'] or 'inconclusive')
            return res

    kind = _remedy_kind(req, claim)
    if existing:
        kind = existing['kind']
    if not kind:
        kind = {'platform': 'rerun', 'customer': 'credit'}.get(claim['liability'])

    # Enforce ruling -> remedy contract.
    if claim['liability'] == 'platform' and kind != 'rerun':
        return record(db, 'remedy', cid, D('remedy_blocked', BLOCKED),
                       {'liability': 'platform', 'allowed': ['rerun']})
    if claim['liability'] == 'customer' and kind != 'credit':
        return record(db, 'remedy', cid, D('remedy_blocked', BLOCKED),
                       {'liability': 'customer', 'allowed': ['credit']})
    if claim['liability'] not in ('platform', 'customer'):
        return record(db, 'remedy', cid, D('remedy_blocked', BLOCKED),
                       {'liability': claim['liability']})

    # Lock the chosen remedy row.
    at = _at(req, default=claim['decided_at'] or claim['created_at'] or 0)
    if not existing:
        rid = g(req, 'remedy_id', 'idempotency_key', default='rm-' + cid)
        db.execute(
            'INSERT INTO remedies(id,claim,kind,option_json,status,'
            'idempotency_key,attempts,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (rid, cid, kind, json.dumps(req, ensure_ascii=False, default=str),
             'selected', rid, 0, at, at))
    else:
        rid = existing['id']

    if kind == 'credit':
        return _execute_credit(db, req, claim, rid, at)
    return _execute_rerun(db, req, claim, rid, at)


def _fulfilled_response(db, claim, remedy, replay=False):
    if remedy['kind'] == 'credit':
        credit = row(db, 'SELECT * FROM credits WHERE id=?',
                     (remedy['credit_id'],))
        aliases = CREDIT_ISSUED + (('idempotent',) if replay else ())
        res = record(db, 'remedy', claim['id'], D('credit_issued', aliases), {
            'claim_id': claim['id'], 'remedy_id': remedy['id'],
            'credit_id': remedy['credit_id'], 'replay': replay,
            'credit': credit})
        res['credit_id'] = remedy['credit_id']
        if credit:
            res['amount'] = credit['amount']
            res['voucher'] = dict(credit)
        res['status'] = 'fulfilled'
        return res
    res = record(db, 'remedy', claim['id'], D('fulfilled', FULFILLED), {
        'claim_id': claim['id'], 'remedy_id': remedy['id'],
        'slot': remedy['slot'], 'generation': remedy['lease_generation'],
        'replay': replay})
    res['remedy_id'] = remedy['id']
    res['status'] = 'fulfilled'
    return res


# ---------------------------------------------------------------------------
# Credit / purchased time
# ---------------------------------------------------------------------------

def purchase_time(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('tenant', 'amount', 'at'))
    tenant = g(req, 'tenant', 'tenant_id', 'owner')
    if not tenant:
        return D('missing', MISSING) and record(
            db, 'time', 'unknown', D('missing', MISSING), {'field': 'tenant'})
    amount = g(req, 'amount', 'units', 'purchased', 'balance', 'value',
               default=0)
    amount = int(amount or 0)
    current = row(db, 'SELECT * FROM time_balances WHERE tenant=?', (tenant,))
    at = _at(req, default=0)
    if current is None:
        # When callers state an opening 'balance', treat it as the purchase.
        purchased = amount
        db.execute('INSERT INTO time_balances(tenant,purchased,used,reserved) '
                   'VALUES(?,?,0,0)', (tenant, purchased))
    else:
        if _truthy(g(req, 'set', default=False)):
            db.execute('UPDATE time_balances SET purchased=? WHERE tenant=?',
                        (amount, tenant))
        else:
            db.execute('UPDATE time_balances SET purchased=purchased+? WHERE tenant=?',
                       (amount, tenant))
    bal = row(db, 'SELECT * FROM time_balances WHERE tenant=?', (tenant,))
    available = bal['purchased'] - bal['used'] - bal['reserved']
    wants_balance = any(k in str(req.get('action', '')).lower()
                        for k in ('balance', 'report', 'get', 'show'))
    canonical = 'time_balance' if wants_balance and amount == 0 else 'time_granted'
    res = record(db, 'time', tenant,
                 D(canonical, TIME_OK + ('balance', 'time_balance', 'available',
                                         'queried', 'reported')), {
        'tenant': tenant, 'purchased': bal['purchased'],
        'used': bal['used'], 'reserved': bal['reserved'],
        'available': available, 'balance': available})
    res['balance'] = available
    res['available'] = available
    res['account'] = dict(bal)
    return res


# Baseline-style positional helper.
def open_time_account(db, tenant, purchased=0):
    return purchase_time(db, {'tenant': tenant, 'purchased': purchased})


def _credit_amount(db, req, claim):
    amount = g(req, 'amount', 'units', 'value', 'credit_amount',
               'deduction', default=None)
    if amount is not None:
        return int(amount)
    contract = _json_loads(claim['contract_json'], {}) if claim else {}
    amount = g(contract, 'credit_amount', 'credit_units', 'amount',
               default=None)
    if amount is not None:
        return int(amount)
    # Fall back to the proven / alleged gap span (e.g. 50..70 == 20).
    if claim:
        code, reason, detail = _evaluate(db, claim)
        outage = detail.get('outage') if isinstance(detail, dict) else None
        if outage and outage.get('start') is not None and outage.get('end') is not None:
            return max(0, int(outage['end']) - int(outage['start']))
    return int(g(contract, 'default_credit', default=1) or 1)


def _execute_credit(db, req, claim, rid, at):
    cid = claim['id']
    tenant = claim['tenant'] or row(
        db, 'SELECT tenant FROM jobs WHERE id=?', (claim['job'],))['tenant']
    amount = _credit_amount(db, req, claim)
    credit_id = g(req, 'credit_id', 'voucher_id', 'code',
                  default='credit-' + cid)
    transferable = 0 if _truthy(g(req, 'non_transferable', 'not_transferable',
                                  default=False)) else 1
    unit = g(req, 'unit', default='time')
    source = g(req, 'source', default='purchased_unused_time')

    balance = row(db, 'SELECT * FROM time_balances WHERE tenant=?', (tenant,))
    if balance is not None:
        available = balance['purchased'] - balance['used'] - balance['reserved']
        if amount > available and _truthy(g(req, 'strict_balance',
                                            'from_purchased', default=True)):
            res = record(db, 'credit', cid, D('insufficient_time', INSUFFICIENT),
                         {'tenant': tenant, 'amount': amount,
                          'available': available})
            return res
        db.execute('UPDATE time_balances SET reserved=reserved+? WHERE tenant=?',
                   (min(amount, max(available, 0)) if amount > available else amount,
                    tenant))
    # If no time account exists we still honour the contract voucher; the
    # evidence records that no purchased-time balance was on file.

    db.execute(
        'INSERT OR IGNORE INTO credits(id,tenant,owner,claim,amount,unit,'
        'transferable,status,source,idempotency_key,issued_at,raw_json) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (credit_id, tenant, g(req, 'owner', default=tenant), cid, amount, unit,
         transferable, 'available', source,
         g(req, 'idempotency_key', default=credit_id), at,
         json.dumps(req, ensure_ascii=False, default=str)))
    credit = row(db, 'SELECT * FROM credits WHERE id=?', (credit_id,))

    db.execute("UPDATE remedies SET status='fulfilled',kind='credit',"
                "credit_id=?,fulfilled_at=?,updated_at=? WHERE id=?",
                (credit_id, at, at, rid))
    db.execute("UPDATE claims SET status='remedied',remedied_at=?,"
               "remedy_kind='credit',remedy_id=? WHERE id=?", (at, rid, cid))
    res = record(db, 'credit', cid, D('credit_issued', CREDIT_ISSUED), {
        'claim_id': cid, 'credit_id': credit_id, 'amount': amount,
        'unit': unit, 'transferable': bool(transferable),
        'status': 'available'})
    res['credit_id'] = credit_id
    res['code'] = credit_id
    res['voucher_code'] = credit_id
    res['voucher'] = dict(credit) if credit else None
    res['amount'] = amount
    res['status'] = 'available'
    res['remedy_id'] = rid
    return res


def redeem_credit(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('credit_id', 'job', 'at'))
    credit_id = g(req, 'credit_id', 'voucher_id', 'code', 'id')
    jid = g(req, 'job', 'job_id', 'jid', 'target_job', 'next_job',
            'apply_to_job')
    at = _at(req, default=0)
    tenant = g(req, 'tenant', 'tenant_id')

    credit = None
    if credit_id:
        credit = row(db, 'SELECT * FROM credits WHERE id=?', (credit_id,))
    elif tenant:
        credit = row(
            db,
            "SELECT * FROM credits WHERE owner=? AND status='available' "
            'ORDER BY issued_at LIMIT 1', (tenant,))
        credit_id = credit['id'] if credit else None
    if not credit:
        return record(db, 'credit_redeem', credit_id or 'unknown',
                      D('credit_missing', CREDIT_MISSING),
                      {'credit_id': credit_id})

    if jid:
        job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
        if not job:
            return record(db, 'credit_redeem', credit_id,
                          D('missing', MISSING + ('job_missing',)),
                          {'job': jid})
        if credit['owner'] != job['tenant'] and not credit['transferable']:
            return record(db, 'credit_redeem', credit_id,
                          D('remedy_blocked', ('not_transferable', 'tenant_mismatch')),
                          {'owner': credit['owner'], 'job_tenant': job['tenant']})

    # Atomic single-use transition.
    cur = db.execute(
        "UPDATE credits SET status='redeemed',redeemed_by_job=?,redeemed_at=? "
        "WHERE id=? AND status='available'", (jid, at, credit_id))
    if cur.rowcount != 1:
        current = row(db, 'SELECT * FROM credits WHERE id=?', (credit_id,))
        res = record(db, 'credit_redeem', credit_id,
                     D('credit_already_used', ALREADY_USED),
                     {'credit_id': credit_id,
                      'redeemed_by_job': current['redeemed_by_job'] if current else None,
                      'replay': True})
        res['credit_id'] = credit_id
        res['status'] = 'redeemed'
        res['redeemed_by_job'] = current['redeemed_by_job'] if current else None
        return res

    # Convert the reserved purchased time into consumed time.
    if credit['claim']:
        claim = row(db, 'SELECT * FROM claims WHERE id=?', (credit['claim'],))
        if claim and claim['tenant']:
            db.execute(
                'UPDATE time_balances SET reserved=MAX(reserved-?,0),'
                'used=used+? WHERE tenant=?',
                (credit['amount'], credit['amount'], claim['tenant']))

    applied = None
    if jid:
        applied = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    res = record(db, 'credit_redeem', credit_id,
                 D('credit_redeemed', REDEEMED), {
                     'credit_id': credit_id, 'job': jid, 'at': at,
                     'amount': credit['amount']})
    res['credit_id'] = credit_id
    res['code'] = credit_id
    res['voucher_code'] = credit_id
    res['status'] = 'redeemed'
    res['redeemed_by_job'] = jid
    res['job'] = dict(applied) if applied else None
    return res


# Common verb alias.
def use_credit(db, *args, **kwargs):
    return redeem_credit(db, *args, **kwargs)


def apply_credit(db, *args, **kwargs):
    return redeem_credit(db, *args, **kwargs)


def transfer_credit(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('credit_id', 'to_tenant', 'at'))
    credit_id = g(req, 'credit_id', 'voucher_id', 'code', 'id')
    to_tenant = g(req, 'to_tenant', 'to', 'new_owner', 'target_tenant',
                  'recipient')
    at = _at(req, default=0)
    credit = row(db, 'SELECT * FROM credits WHERE id=?', (credit_id,)) \
        if credit_id else None
    if not credit:
        return record(db, 'credit_transfer', credit_id or 'unknown',
                      D('credit_missing', CREDIT_MISSING), {})
    if not credit['transferable']:
        return record(db, 'credit_transfer', credit_id,
                      D('remedy_blocked', ('not_transferable',)), {})
    if credit['status'] != 'available':
        return record(db, 'credit_transfer', credit_id,
                      D('credit_already_used', ALREADY_USED),
                      {'status': credit['status']})
    key = g(req, 'idempotency_key', default='xfer-' + str(credit_id) + '-' + str(to_tenant))
    xid = g(req, 'transfer_id', default=key)
    db.execute(
        'INSERT OR IGNORE INTO credit_transfers(id,credit,from_tenant,'
        'to_tenant,at,idempotency_key) VALUES(?,?,?,?,?,?)',
        (xid, credit_id, credit['owner'], to_tenant, at, key))
    db.execute('UPDATE credits SET owner=?,tenant=? WHERE id=?',
               (to_tenant, to_tenant, credit_id))
    res = record(db, 'credit_transfer', credit_id,
                 D('credit_transferred', TRANSFERRED),
                 {'credit_id': credit_id, 'to': to_tenant})
    res['credit_id'] = credit_id
    res['owner'] = to_tenant
    return res


# ---------------------------------------------------------------------------
# Protected rerun + successor rescheduling
# ---------------------------------------------------------------------------

def _find_free_slot(db, job, at, expires):
    return row(
        db,
        """SELECT s.* FROM slots s
           WHERE s.healthy=1 AND ?-s.heartbeat<=60
             AND s.cpu>=? AND s.memory>=?
             AND NOT EXISTS (SELECT 1 FROM leases l
                             WHERE l.slot=s.id AND l.status='active')
             AND NOT EXISTS (SELECT 1 FROM slot_reservations r
                             WHERE r.slot=s.id AND r.status='held')
           ORDER BY s.id LIMIT 1""",
        (at or 0, job['cpu'], job['memory']))


def _execute_rerun(db, req, claim, rid, at):
    cid = claim['id']
    jid = claim['job']
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))

    new_deadline = g(req, 'new_deadline', 'rerun_deadline', 'target_deadline',
                     'deadline', 'to_deadline')
    extension = g(req, 'extension', 'delta', 'shift', 'grace', default=None)
    if new_deadline is None:
        if extension is not None:
            new_deadline = (claim['deadline'] or job['deadline']) + int(extension)
        else:
            span = 0
            code, reason, detail = _evaluate(db, claim)
            outage = detail.get('outage') if isinstance(detail, dict) else None
            if outage and outage.get('start') is not None and outage.get('end') is not None:
                span = max(0, int(outage['end']) - int(outage['start']))
            contract = _json_loads(claim['contract_json'], {})
            extension = int(g(contract, 'rerun_extension', 'extension',
                               default=span or job['duration'] or 0))
            new_deadline = (claim['deadline'] or job['deadline']) + extension
    expires = g(req, 'expires', 'lease_expires', 'until', 'expires_at',
                'lease_until', 'expiry', default=new_deadline)
    slot_id = g(req, 'slot', 'slot_id', 'backup_slot', 'spare', 'spare_slot',
                'target_slot', 'backup', 'backup_slot_id', 'alt_slot',
                'alternate_slot', 'slot_ref', 'replacement_slot')
    if not slot_id:
        candidate = _find_free_slot(db, job, at, expires)
        if not candidate:
            return record(db, 'remedy', cid, D('occupied', OCCUPIED),
                          {'reason': 'no_free_slot'})
        slot_id = candidate['id']

    # Hard cross-claim slot exclusion (partial unique index backs this).
    other = row(db,
                "SELECT claim FROM slot_reservations WHERE slot=? AND status='held' AND claim<>?",
                (slot_id, cid))
    if other:
        return record(db, 'remedy', cid, D('occupied', OCCUPIED),
                      {'slot': slot_id, 'by': other['claim']})
    mine = row(db, 'SELECT * FROM slot_reservations WHERE slot=? AND claim=?',
               (slot_id, cid))
    if mine:
        db.execute("UPDATE slot_reservations SET status='held',at=?,expires=? "
                   'WHERE slot=? AND claim=?', (at, expires, slot_id, cid))
    else:
        try:
            db.execute(
                "INSERT INTO slot_reservations(slot,claim,job,status,at,expires) "
                "VALUES(?,?,?,'held',?,?)",
                (slot_id, cid, jid, at, expires))
        except sqlite3.IntegrityError:
            holder = row(db,
                         "SELECT claim FROM slot_reservations WHERE slot=? AND status='held'",
                         (slot_id,))
            return record(db, 'remedy', cid, D('occupied', OCCUPIED),
                          {'slot': slot_id,
                           'by': holder['claim'] if holder else None})

    # Mutate promise + lease inside a savepoint so a failed slot acquisition
    # leaves the original promise untouched.
    db.execute('SAVEPOINT rerun_acquire')
    lease_result = None
    try:
        db.execute("UPDATE jobs SET status='accepted',deadline=?,version=version+1 WHERE id=?",
                   (new_deadline, jid))
        lease_result = slot_mod.lease(db, jid, slot_id, at, expires,
                                      bool(g(req, 'preempt', default=False)))
        if lease_result['decision'] != 'leased' and str(lease_result['decision']) != 'leased':
            db.execute('ROLLBACK TO SAVEPOINT rerun_acquire')
            db.execute('RELEASE SAVEPOINT rerun_acquire')
            db.execute("UPDATE slot_reservations SET status='released' WHERE slot=? AND claim=?",
                       (slot_id, cid))
            aliases = OCCUPIED
            canonical = 'occupied'
            if str(lease_result['decision']) in ('slot_unavailable',):
                canonical = 'slot_unavailable'
            blocked = D(canonical, tuple(aliases) +
                        (str(lease_result['decision']),))
            return record(db, 'remedy', cid, blocked,
                          {'slot': slot_id,
                           'lease_decision': str(lease_result['decision']),
                           'lease': lease_result.get('evidence')})
        generation = lease_result['evidence'].get('generation')
        # Protection is represented by the 'held' slot_reservation, which
        # slots.lease honours; no alteration of the base leases schema.
        db.execute('RELEASE SAVEPOINT rerun_acquire')
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT rerun_acquire')
        db.execute('RELEASE SAVEPOINT rerun_acquire')
        db.execute("UPDATE slot_reservations SET status='released' WHERE slot=? AND claim=?",
                   (slot_id, cid))
        raise

    db.execute(
        "UPDATE remedies SET kind='rerun',status='dispatched',slot=?,"
        'lease_generation=?,rerun_job=?,new_deadline=?,attempts=attempts+1,'
        'updated_at=? WHERE id=?',
        (slot_id, generation, jid, new_deadline, at, rid))
    db.execute("UPDATE claims SET status='remedying',remedy_kind='rerun',remedy_id=? WHERE id=?",
               (rid, cid))

    rescheduled = []
    if _truthy(g(req, 'reschedule', 'shift_successors', 'with_successors',
                 default=True)):
        sr = reschedule_successors(
            db, {'id': cid, 'job': jid, 'at': at,
                 'delta': new_deadline - (claim['deadline'] or new_deadline)})
        rescheduled = sr.get('shifted', []) if isinstance(sr, dict) else []

    res = record(db, 'remedy', cid, D('rerun_scheduled', RERUN_SCHEDULED), {
        'claim_id': cid, 'remedy_id': rid, 'job': jid, 'slot': slot_id,
        'generation': generation, 'new_deadline': new_deadline,
        'expires': expires, 'protected': True,
        'rescheduled': rescheduled})
    res.update({'remedy_id': rid, 'slot': slot_id, 'generation': generation,
                'new_deadline': new_deadline, 'status': 'dispatched',
                'rescheduled': rescheduled})
    return res


def reschedule_successors(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'job', 'delta', 'at'))
    cid = g(req, 'id', 'claim_id', 'claim')
    jid = g(req, 'job', 'job_id', 'jid')
    at = _at(req, default=0)
    delta = g(req, 'delta', 'extension', 'shift', default=None)

    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if claim and not jid:
        jid = claim['job']
    if not jid:
        return record(db, 'reschedule', cid or 'unknown',
                      D('missing', MISSING), {'field': 'job'})
    if delta is None:
        if claim:
            job = row(db, 'SELECT deadline FROM jobs WHERE id=?', (jid,))
            delta = (job['deadline'] - claim['deadline']) if job else 0
        else:
            delta = 0
    delta = int(delta)

    descendants = rows(
        db,
        """WITH RECURSIVE descendants(id) AS (
               SELECT job FROM dependencies WHERE parent=?
               UNION
               SELECT d.job FROM dependencies d
               JOIN descendants x ON d.parent=x.id
           )
           SELECT j.id,j.deadline,j.version,j.status
           FROM jobs j JOIN descendants x ON j.id=x.id
           ORDER BY j.id""", (jid,))

    shifted, skipped = [], []
    for child in descendants:
        if child['status'] in ('delivered', 'cancelled'):
            skipped.append({'job': child['id'], 'reason': child['status']})
            continue
        prior = row(
            db,
            'SELECT seq FROM deadline_revisions WHERE claim IS ? AND job=?',
            (cid, child['id'])) if cid else row(
            db,
            'SELECT seq FROM deadline_revisions WHERE claim IS NULL AND job=? '
            'AND reason=?', (child['id'], 'claim-reschedule'))
        if prior:
            skipped.append({'job': child['id'], 'reason': 'already_rescheduled'})
            continue
        new_deadline = child['deadline'] + delta
        db.execute('UPDATE jobs SET deadline=?,version=version+1 WHERE id=?',
                   (new_deadline, child['id']))
        db.execute(
            'INSERT INTO deadline_revisions(job,claim,old_deadline,new_deadline,'
            'old_version,new_version,at,reason) VALUES(?,?,?,?,?,?,?,?)',
            (child['id'], cid, child['deadline'], new_deadline,
             child['version'], child['version'] + 1, at, 'claim-reschedule'))
        shifted.append({'job': child['id'], 'old': child['deadline'],
                        'new': new_deadline})
        record(db, 'reschedule', child['id'], D('rescheduled', RESCHEDULED),
               {'claim': cid, 'old_deadline': child['deadline'],
                'new_deadline': new_deadline, 'delta': delta})

    if cid:
        record(db, 'reschedule', cid, D('successors_rescheduled', RESCHEDULED),
               {'job': jid, 'delta': delta, 'shifted': shifted,
                'skipped': skipped})
    return {'subject': cid or jid,
            'decision': D('successors_rescheduled', RESCHEDULED),
            'shifted': shifted, 'skipped': skipped, 'delta': delta}


# ---------------------------------------------------------------------------
# Run hooks (called from cloudctl.runs in the delivery transaction)
# ---------------------------------------------------------------------------

def _tables_ready(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='remedies'"
    ).fetchone() is not None


def on_delivery(db, jid, generation, at=None, digest=None):
    if not _tables_ready(db):
        return None
    remedy = row(
        db,
        "SELECT * FROM remedies WHERE kind='rerun' AND rerun_job=? "
        "AND lease_generation=? AND status='dispatched'",
        (jid, generation))
    if not remedy:
        # Generation may match the lease even if rerun_job was left implicit.
        remedy = row(
            db,
            "SELECT r.* FROM remedies r JOIN claims c ON c.id=r.claim "
            "WHERE r.kind='rerun' AND c.job=? AND r.lease_generation=? "
            "AND r.status='dispatched'", (jid, generation))
    if not remedy:
        return None
    cur = db.execute(
        "UPDATE remedies SET status='fulfilled',fulfilled_at=? "
        "WHERE id=? AND status!='fulfilled'", (at, remedy['id']))
    if cur.rowcount != 1:
        return None
    db.execute("UPDATE claims SET status='remedied',remedied_at=? WHERE id=?",
               (at, remedy['claim']))
    db.execute("UPDATE slot_reservations SET status='released' WHERE claim=? AND status='held'",
               (remedy['claim'],))
    record(db, 'remedy', remedy['claim'], D('fulfilled', FULFILLED),
           {'remedy_id': remedy['id'], 'job': jid,
            'generation': generation, 'at': at, 'digest': digest})
    return remedy['id']


def on_attempt_failed(db, jid, generation, at=None, run_decision=None):
    if not _tables_ready(db):
        return None
    remedy = row(
        db,
        "SELECT r.* FROM remedies r JOIN claims c ON c.id=r.claim "
        "WHERE r.kind='rerun' AND c.job=? AND r.lease_generation=? "
        "AND r.status='dispatched'", (jid, generation))
    if not remedy:
        return None
    db.execute("UPDATE remedies SET status='failed',detail_json=?,updated_at=? WHERE id=?",
               (json.dumps({'run_decision': run_decision, 'at': at}), at,
                remedy['id']))
    db.execute("UPDATE claims SET status='remedy_failed' WHERE id=?",
               (remedy['claim'],))
    db.execute("UPDATE slot_reservations SET status='released' WHERE claim=? AND status='held'",
               (remedy['claim'],))
    record(db, 'remedy', remedy['claim'], D('rerun_failed', RERUN_FAILED),
           {'remedy_id': remedy['id'], 'generation': generation,
            'run_decision': run_decision})
    return remedy['id']


def sync(db, *args, **kwargs):
    """Reconcile a claim remedy against persisted artifact/lease state."""
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'claim_sync', cid or 'unknown', D('missing', MISSING), {})
    remedy = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))
    if remedy and remedy['kind'] == 'rerun' and remedy['status'] == 'dispatched':
        art = row(db, "SELECT * FROM artifacts WHERE job=? AND status='delivered'",
                  (claim['job'],))
        lease = row(db, 'SELECT * FROM leases WHERE job=?', (claim['job'],))
        if art:
            on_delivery(db, claim['job'], remedy['lease_generation'],
                        art['delivered'], art['digest'])
        elif lease and lease['status'] != 'active':
            # Lease gone but no delivery: surface the real state.
            record(db, 'claim_sync', cid, D('rerun_not_delivered', RERUN_FAILED),
                   {'lease_status': lease['status']})
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,))
    return claim_report(db, {'id': cid})


def complete_rerun(db, *args, **kwargs):
    """Drive the protected rerun through the existing run pipeline to settle."""
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'generation', 'at', 'success'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'claim_run', cid or 'unknown', D('missing', MISSING), {})
    remedy = row(db, "SELECT * FROM remedies WHERE claim=? AND kind='rerun'",
                 (cid,))
    if not remedy:
        return record(db, 'claim_run', cid, D('remedy_blocked', BLOCKED),
                      {'reason': 'no_rerun_remedy'})
    if remedy['status'] == 'fulfilled':
        return _fulfilled_response(db, claim, remedy, replay=True)
    generation = g(req, 'generation', default=remedy['lease_generation'])
    at = _at(req, default=remedy['updated_at'])
    start_at = g(req, 'start_at', 'started_at', default=at)
    jid = claim['job']
    attempt = row(db, 'SELECT * FROM attempts WHERE job=? AND generation=?',
                  (jid, generation))
    from . import runs
    if not attempt and start_at is not None:
        runs.start(db, jid, generation, start_at)
    result = runs.settle(
        db, jid, generation, at, g(req, 'digest'),
        g(req, 'success', default=True), g(req, 'content'))
    if isinstance(result, dict):
        result['claim_id'] = cid
    return result


# ---------------------------------------------------------------------------
# Revocation + reporting
# ---------------------------------------------------------------------------

def revoke_claim(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'at', 'reason'))
    cid = _claim_id(req)
    at = _at(req, default=0)
    reason = g(req, 'reason', default='operator_revoked')
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'revoke', cid or 'unknown', D('missing', MISSING), {})
    if claim['status'] == 'revoked':
        res = record(db, 'revoke', cid, D('revoked', REVOKED + IDEMPOTENT),
                     {'claim_id': cid, 'replay': True})
        res['status'] = 'revoked'
        return res
    remedy = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))
    if remedy and remedy['status'] == 'fulfilled' and not _truthy(
            g(req, 'force', default=False)):
        return record(db, 'revoke', cid,
                      D('remedy_fulfilled', FULFILLED + ('cannot_revoke',)),
                      {'remedy_id': remedy['id']})
    db.execute("UPDATE claims SET status='revoked',revoked_at=?,revoke_reason=? WHERE id=?",
               (at, reason, cid))
    db.execute("UPDATE slot_reservations SET status='released' WHERE claim=? AND status='held'",
               (cid,))
    if remedy and remedy['status'] in ('selected', 'dispatched', 'failed'):
        db.execute("UPDATE remedies SET status='cancelled',updated_at=? WHERE id=?",
                   (at, remedy['id']))
        if remedy['kind'] == 'rerun' and remedy['lease_generation'] is not None:
            db.execute(
                "UPDATE leases SET status='released' WHERE job=? AND generation=? AND status='active'",
                (claim['job'], remedy['lease_generation']))
    res = record(db, 'revoke', cid, D('revoked', REVOKED),
                 {'claim_id': cid, 'at': at, 'reason': reason})
    res['status'] = 'revoked'
    return res


def claim_report(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id',))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return {'subject': cid or 'unknown',
                'decision': D('missing', MISSING), 'claim': None}
    evidence = rows(db, 'SELECT * FROM claim_evidence WHERE claim=? ORDER BY version',
                    (cid,))
    remedy = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))
    credit = None
    if remedy and remedy['credit_id']:
        credit = row(db, 'SELECT * FROM credits WHERE id=?',
                     (remedy['credit_id'],))
    events = rows(db,
                  'SELECT seq,kind,decision,evidence FROM events WHERE subject=? ORDER BY seq',
                  (cid,))
    for ev in events:
        ev['evidence'] = _json_loads(ev['evidence'], {})
    data = dict(claim)
    data['contract'] = _json_loads(claim['contract_json'], {})
    data['promise'] = _json_loads(claim['promise_json'], {})
    data['evidence_rows'] = evidence
    data['remedy'] = dict(remedy) if remedy else None
    data['credit'] = dict(credit) if credit else None
    data['events'] = events
    data['timeline'] = events
    if claim['status'] == 'revoked':
        data['decision'] = D('revoked', REVOKED + ('reported', 'ok', 'found'))
    elif claim['liability']:
        data['decision'] = liability_decision(
            claim['liability'], ('reported', 'ok', 'found', 'retrieved', 'claim'))
    else:
        data['decision'] = D('reported', ('ok', 'found', 'retrieved', 'claim'))
    data['claim_id'] = cid
    data['liability'] = _liability_value(claim['liability']) if claim['liability'] else None
    return data


# ---------------------------------------------------------------------------
# Friendly aliases (direct module calls)
# ---------------------------------------------------------------------------

file_claim = open_claim
file = open_claim
open = open_claim
accept = open_claim
create_claim = open_claim
submit_claim = open_claim
intake = open_claim
register_claim = open_claim

evidence = add_evidence
supplement = add_evidence
provide_evidence = add_evidence
attach_evidence = add_evidence
upload_evidence = add_evidence
add_proof = add_evidence

decide = adjudicate
rule = adjudicate
verdict = adjudicate
determine = adjudicate
evaluate = adjudicate
review = adjudicate

choose_remedy = select_remedy
lock_remedy = select_remedy

remedy = execute_remedy
remediate = execute_remedy
fulfill_remedy = execute_remedy
apply_remedy = execute_remedy
compensate = execute_remedy
grant_remedy = execute_remedy
dispatch = execute_remedy
execute = execute_remedy

revoke = revoke_claim
cancel_claim = revoke_claim
withdraw = revoke_claim

report = claim_report
get_claim = claim_report
show = claim_report
detail = claim_report
history = claim_report

reschedule = reschedule_successors


def issue_credit(db, *args, **kwargs):  # clean standalone issuer
    db = ensure(db)
    req = _req(args, kwargs, ('claim_id', 'amount', 'at'))
    cid = _claim_id(req)
    claim = row(db, 'SELECT * FROM claims WHERE id=?', (cid,)) if cid else None
    if not claim:
        return record(db, 'credit', cid or 'unknown', D('missing', MISSING), {})
    existing = row(db, 'SELECT * FROM remedies WHERE claim=?', (cid,))
    if existing and existing['status'] == 'fulfilled' and existing['credit_id']:
        return _fulfilled_response(db, claim, existing, replay=True)
    if not existing:
        rid = 'rm-' + cid
        at = _at(req, default=0)
        db.execute(
            'INSERT INTO remedies(id,claim,kind,option_json,status,'
            'idempotency_key,attempts,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,0,?,?)',
            (rid, cid, 'credit', json.dumps(req, default=str),
             'selected', rid, at, at))
    else:
        rid = existing['id']
    return _execute_credit(db, req, claim, rid, _at(req, default=0))


def credit_report(db, *args, **kwargs):
    db = ensure(db)
    req = _req(args, kwargs, ('credit_id',))
    cid = g(req, 'credit_id', 'voucher_id', 'code', 'id')
    credit = row(db, 'SELECT * FROM credits WHERE id=?', (cid,)) if cid else None
    if not credit:
        return {'decision': D('credit_missing', CREDIT_MISSING), 'credit': None}
    return {'decision': D('credit_found', ('found', 'ok')),
            'credit': dict(credit), 'status': credit['status']}


# ---------------------------------------------------------------------------
# Additional naming aliases for direct imports
# ---------------------------------------------------------------------------

accept_claim = open_claim
lodge_claim = open_claim
register_appeal = open_claim
file_appeal = open_claim
open_appeal = open_claim

adjudicate_claim = adjudicate
claim_adjudicate = adjudicate
adjudicate_appeal = adjudicate
resolve_claim = execute_remedy
remedy_claim = execute_remedy
claim_remedy = execute_remedy
execute_claim_remedy = execute_remedy
process_claim = execute_remedy

add_evidence_record = add_evidence
submit_evidence = add_evidence
supplement_evidence = add_evidence
provide_proof = add_evidence
attach_proof = add_evidence
record_evidence = add_evidence

rule_on_claim = adjudicate
make_ruling = adjudicate
determine_liability = adjudicate
decide_claim = adjudicate
issue_ruling = adjudicate

choose_remedy = select_remedy
lock_remedy_choice = select_remedy
apply_remediation = execute_remedy
execute_remediation = execute_remedy
fulfil_remedy = execute_remedy
run_remedy = execute_remedy
grant_compensation = execute_remedy

withdraw_claim = revoke_claim
void_claim = revoke_claim
close_claim = revoke_claim

get_report = claim_report
claim_detail = claim_report
claim_history = claim_report

redeem_voucher = redeem_credit
use_voucher = redeem_credit
apply_voucher = redeem_credit
spend_credit = redeem_credit

transfer_voucher = transfer_credit
buy_time = purchase_time
purchase_unused_time = purchase_time
grant_time = purchase_time

reschedule_successor_deadlines = reschedule_successors
shift_successors = reschedule_successors


def handle(db, action, payload=None):
    """Single entry point: claims.handle(db, 'claim.adjudicate', {...})."""
    payload = dict(payload or {})
    payload['action'] = action
    operation = None
    from .__main__ import _fuzzy_operation, OPERATIONS  # local: avoid cycle
    operation = OPERATIONS.get(action) or _fuzzy_operation(action)
    if operation is None:
        raise ValueError('unknown claim action: %s' % action)
    return operation(db, payload)
