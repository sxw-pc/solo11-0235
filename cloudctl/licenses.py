"""Software-seat licensing: real borrow, return and compliance audit.

A software seat is *not* a CPU slot. This module keeps the whole license
boundary recoverable: entitlement validation (product/version/region/tenant),
remote token identity, attempt generation, return proof and delivery
eligibility. Every vendor interaction is captured as an immutable receipt;
corrections are appended, never overwriting the original record. Seats that
cannot be proven free stay occupied or quarantined.
"""
import json
from .store import row, rows, record
from .license_gateway import ServiceUnavailable, get_gateway

# Grant lifecycle. Every transition is kept as an event; receipts and
# corrections are append-only.
HELD = 'held'
RETURN_PENDING = 'return_pending'
RETURNED = 'returned'
TIMED_OUT = 'timed_out'
QUARANTINED = 'quarantined'
CANCELLED = 'cancelled'
REJECTED = 'rejected'
EMERGENCY = 'emergency'

TERMINAL = {RETURNED, TIMED_OUT, CANCELLED}
OCCUPYING = {HELD, RETURN_PENDING, QUARANTINED, EMERGENCY}


# --------------------------------------------------------------------------
# Entitlement registration and window coverage
# --------------------------------------------------------------------------

def register_entitlement(db, request):
    eid = request['id']
    old = row(db, 'SELECT * FROM entitlements WHERE id=?', (eid,))
    if old:
        return record(db, 'license.entitlement', eid, 'conflict',
                      {'old_status': old['status']})
    if min(request['seats'], request['borrowed_until'] - request['borrowed_from']) <= 0:
        return record(db, 'license.entitlement', eid, 'invalid_contract', {})
    kind = request.get('kind', 'byol' if request.get('tenant') else 'operator')
    if kind == 'byol' and not request.get('tenant'):
        return record(db, 'license.entitlement', eid, 'invalid_contract',
                      {'reason': 'byol requires tenant'})
    if kind == 'operator' and request.get('tenant'):
        return record(db, 'license.entitlement', eid, 'invalid_contract',
                      {'reason': 'operator backup cannot be tenant scoped'})
    db.execute('INSERT INTO entitlements VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (eid, request['product'], str(request['version']),
                request['region'], request.get('tenant'), kind,
                request['seats'], request['borrowed_from'],
                request['borrowed_until'], 'active', None))
    db.executemany('INSERT INTO windows VALUES(?,?,?)',
                   [(eid, w['start'], w['end'])
                    for w in request.get('windows',
                                         [{'start': request['borrowed_from'],
                                           'end': request['borrowed_until']}])])
    gateway = get_gateway(db)
    if hasattr(gateway, 'register_capacity'):
        gateway.register_capacity(eid, request['seats'])
    return record(db, 'license.entitlement', eid, 'registered',
                  {'product': request['product'], 'version': str(request['version']),
                   'region': request['region'], 'kind': kind,
                   'seats': request['seats'],
                   'tenant': request.get('tenant')})


def cancel_entitlement(db, eid, at, reason):
    ent = row(db, 'SELECT * FROM entitlements WHERE id=?', (eid,))
    if not ent:
        return record(db, 'license.entitlement', eid, 'missing', {})
    if ent['status'] == 'cancelled':
        return record(db, 'license.entitlement', eid, 'idempotent', {})
    outstanding = rows(db, "SELECT id FROM grants WHERE entitlement=? AND status IN ('held','emergency','return_pending','quarantined')",
                       (eid,))
    live = [g['id'] for g in outstanding]
    if live:
        return record(db, 'license.entitlement', eid, 'seats_outstanding',
                      {'grants': live})
    # Historical grants under this contract are marked remote-cancelled so a
    # late vendor receipt can never re-legalise them.
    db.execute('UPDATE grants SET remote_cancelled=1 WHERE entitlement=?', (eid,))
    db.execute('UPDATE entitlements SET status=?, cancelled_at=? WHERE id=?',
               ('cancelled', at, eid))
    return record(db, 'license.entitlement', eid, 'cancelled', {'reason': reason})


def _windows(db, eid):
    return [(w['start_ts'], w['end_ts'])
            for w in rows(db, 'SELECT start_ts,end_ts FROM windows WHERE entitlement=? ORDER BY start_ts', (eid,))]


def _covering_windows(db, eid, start, end):
    """Return contiguous windows covering [start, end], else None.

    A single attempt crossing two windows must hold one valid right across
    the whole interval; if no entitlement covers the full span it must be
    re-approved at the boundary.
    """
    cover, cursor = [], start
    for w_start, w_end in _windows(db, eid):
        if w_start <= cursor < w_end and w_end >= end:
            return cover + [(w_start, w_end)]
        if w_start <= cursor < w_end:
            cover.append((w_start, w_end))
            cursor = w_end
            if cursor >= end:
                return cover
    return None


def _valid_entitlements(db, job, product, version, region, tenant, start, end):
    out = []
    for ent in rows(db, 'SELECT * FROM entitlements WHERE product=? AND status=?',
                    (product, 'active')):
        if str(ent['version']) != str(version):
            continue
        if ent['region'] != region:
            continue
        # BYOL seats may only serve their owning tenant; operator backup may
        # serve anyone but never gets re-lent to another tenant's name.
        if ent['kind'] == 'byol' and ent['tenant'] != tenant:
            continue
        if not _covering_windows(db, ent['id'], start, end):
            continue
        out.append(ent)
    # Prefer the customer's own contract over operator-paid backup.
    out.sort(key=lambda e: 0 if e['kind'] == 'byol' else 1)
    return out


def _local_used(db, eid):
    """Seats we cannot prove free: held, return-pending and quarantined."""
    return len(rows(db, "SELECT id FROM grants WHERE entitlement=? AND status IN ('held','return_pending','quarantined','emergency')", (eid,)))


# --------------------------------------------------------------------------
# Receipts and corrections (append-only)
# --------------------------------------------------------------------------

def _store_receipt(db, payload, received_at, stale=False):
    db.execute('INSERT OR IGNORE INTO license_receipts'
               '(receipt_id,op,grant,entitlement,token,status,payload,received_at,stale)'
               ' VALUES(?,?,?,?,?,?,?,?,?)',
               (payload.get('receipt_id'), payload.get('op'),
                payload.get('grant'), payload.get('entitlement'),
                payload.get('token'), payload.get('status'),
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
                received_at, int(stale)))
    return row(db, 'SELECT * FROM license_receipts WHERE receipt_id=? AND op=?',
               (payload.get('receipt_id'), payload.get('op')))


def _store_correction(db, correction_id, kind, grant, eid, reference, at, payload):
    db.execute('INSERT OR IGNORE INTO license_corrections'
               '(correction_id,kind,grant,entitlement,reference,at,payload)'
               ' VALUES(?,?,?,?,?,?,?)',
               (correction_id, kind, grant, eid, reference, at,
                json.dumps(payload, sort_keys=True, ensure_ascii=False)))
    return row(db, 'SELECT * FROM license_corrections WHERE correction_id=?',
               (correction_id,))


# --------------------------------------------------------------------------
# Borrowing: the seat is occupied only when the vendor actually hands back
# a token. A lost response leaves a pending record, never a silent re-borrow.
# --------------------------------------------------------------------------

def _grant_id(job, generation):
    return f'grant-{job}-{generation}'


def _request_id(job, generation, eid):
    return f'req-{eid}-{job}-{generation}'


def borrow(db, request):
    jid, generation, at = request['id'], request['generation'], request['at']
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not job:
        return record(db, 'license.borrow', jid, 'missing', {})
    need = row(db, 'SELECT * FROM job_license WHERE job=?', (jid,))
    if not need:
        return record(db, 'license.borrow', jid, 'no_license_requirement', {})
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if not lease or lease['status'] != 'active' or lease['generation'] != generation:
        return record(db, 'license.borrow', jid, 'not_leased',
                      {'generation': generation})
    gid = _grant_id(jid, generation)
    existing = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if existing and existing['status'] in (HELD, EMERGENCY):
        return record(db, 'license.borrow', jid, 'idempotent',
                      {'grant': gid, 'token': existing['token'],
                       'status': existing['status']})
    if existing:
        return record(db, 'license.borrow', jid, 'grant_not_reusable',
                      {'grant': gid, 'status': existing['status']})
    start, end = at, request.get('until', lease['expires'])
    if end <= start:
        return record(db, 'license.borrow', jid, 'invalid_window', {})
    preferred = request.get('entitlement')
    ents = _valid_entitlements(db, job, need['product'], need['version'],
                               need['region'], job['tenant'], start, end)
    if preferred:
        picked = next((e for e in ents if e['id'] == preferred), None)
        if picked is None:
            return record(db, 'license.borrow', jid, 'entitlement_invalid',
                          {'entitlement': preferred})
        ents = [picked]
    if not ents:
        return record(db, 'license.borrow', jid, 'no_entitlement',
                      {'product': need['product'], 'version': need['version'],
                       'region': need['region']})
    gateway = get_gateway(db)
    last = None
    for ent in ents:
        if _local_used(db, ent['id']) >= ent['seats']:
            last = ('local_capacity', ent['id'])
            continue
        req_id = _request_id(jid, generation, ent['id'])
        try:
            receipt = gateway.borrow(ent['id'], jid, generation, req_id, at)
        except ServiceUnavailable:
            # The vendor may already have lent the seat. Record an open
            # request instead of retrying against another contract.
            db.execute('INSERT INTO grants(id,entitlement,job,generation,tenant,responsibility,request_id,token,status,acquired_at,expires_at,released_at,return_until,remote_cancelled,reason)'
                       ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (gid, ent['id'], jid, generation, job['tenant'],
                        ent['kind'], req_id, None, QUARANTINED, None, end,
                        None, None, 0, 'borrow_response_lost'))
            return record(db, 'license.borrow', jid, 'service_unavailable_pending',
                          {'grant': gid, 'entitlement': ent['id'],
                           'request_id': req_id})
        stored = _store_receipt(db, receipt, at)
        if receipt['status'] != 'granted':
            last = ('vendor_rejected', ent['id'], receipt.get('reason'))
            continue
        responsibility = 'customer' if ent['kind'] == 'byol' else 'operator'
        db.execute('INSERT INTO grants(id,entitlement,job,generation,tenant,responsibility,request_id,token,status,acquired_at,expires_at,released_at,return_until,remote_cancelled,reason)'
                   ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (gid, ent['id'], jid, generation, job['tenant'],
                    responsibility, req_id, receipt['token'], HELD, at, end,
                    None, None, 0, ''))
        db.executemany('INSERT INTO grant_windows VALUES(?,?,?,?)',
                       [(gid, w[0], w[1], ent['id'])
                        for w in _covering_windows(db, ent['id'], start, end)])
        return record(db, 'license.borrow', jid, 'borrowed',
                      {'grant': gid, 'entitlement': ent['id'],
                       'token': receipt['token'],
                       'receipt_id': receipt['receipt_id'],
                       'responsibility': responsibility,
                       'receipt_seq': stored['seq']})
    return record(db, 'license.borrow', jid, 'no_seat_available',
                  {'last': last})


def emergency_borrow(db, request):
    """Operator emergency borrow while the vendor is unreachable.

    Reserved operator seats only; the operator pays. Raw paper trail is kept
    and reconciled as soon as the vendor is reachable again.
    """
    jid, generation, at = request['id'], request['generation'], request['at']
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    need = row(db, 'SELECT * FROM job_license WHERE job=?', (jid,))
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if not job or not need:
        return record(db, 'license.emergency', jid, 'missing', {})
    if not lease or lease['status'] != 'active' or lease['generation'] != generation:
        return record(db, 'license.emergency', jid, 'not_leased', {'generation': generation})
    gid = _grant_id(jid, generation)
    existing = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if existing and existing['status'] in (HELD, EMERGENCY):
        return record(db, 'license.emergency', jid, 'idempotent',
                      {'grant': gid, 'status': existing['status']})
    if existing:
        return record(db, 'license.emergency', jid, 'grant_not_reusable',
                      {'grant': gid, 'status': existing['status']})
    end = request.get('until', lease['expires'])
    ents = _valid_entitlements(db, job, need['product'], need['version'],
                               need['region'], job['tenant'], at, end)
    backup = next((e for e in ents if e['kind'] == 'operator'), None)
    if backup is None:
        return record(db, 'license.emergency', jid, 'no_operator_backup', {})
    if _local_used(db, backup['id']) >= backup['seats']:
        return record(db, 'license.emergency', jid, 'reserve_exhausted',
                      {'entitlement': backup['id']})
    token = f'emergency-{backup["id"]}-{jid}-{generation}'
    req_id = _request_id(jid, generation, backup['id'])
    db.execute('INSERT INTO grants(id,entitlement,job,generation,tenant,responsibility,request_id,token,status,acquired_at,expires_at,released_at,return_until,remote_cancelled,reason)'
               ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (gid, backup['id'], jid, generation, job['tenant'], 'operator',
                req_id, token, EMERGENCY, at, end, None, None, 0,
                request.get('reason', 'manual_emergency')))
    db.executemany('INSERT INTO grant_windows VALUES(?,?,?,?)',
                   [(gid, w[0], w[1], backup['id'])
                    for w in _covering_windows(db, backup['id'], at, end)])
    _store_correction(db, f'emerg-{jid}-{generation}', 'emergency_borrow',
                      gid, backup['id'], req_id, at,
                      {'token': token, 'reason': request.get('reason', 'manual_emergency'),
                       'operator': request.get('operator', 'unknown')})
    return record(db, 'license.emergency', jid, 'emergency_borrowed',
                  {'grant': gid, 'entitlement': backup['id'], 'token': token,
                   'responsibility': 'operator'})


# --------------------------------------------------------------------------
# Re-approval at a window boundary
# --------------------------------------------------------------------------

def reauthorize(db, request):
    jid, generation, at = request['id'], request['generation'], request['at']
    gid = _grant_id(jid, generation)
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if not grant or grant['status'] not in (HELD, EMERGENCY):
        return record(db, 'license.reauthorize', jid, 'no_active_grant',
                      {'grant': gid})
    need = row(db, 'SELECT * FROM job_license WHERE job=?', (jid,))
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not need or not job:
        return record(db, 'license.reauthorize', jid, 'missing', {})
    end = request.get('until', grant['expires_at'])
    ents = _valid_entitlements(db, job, need['product'], need['version'],
                               need['region'], job['tenant'], at, end)
    same = next((e for e in ents if e['id'] == grant['entitlement']), None)
    if same is not None:
        cover = _covering_windows(db, same['id'], at, end)
        db.executemany('INSERT OR IGNORE INTO grant_windows VALUES(?,?,?,?)',
                       [(gid, w[0], w[1], same['id']) for w in cover])
        # The same right remains valid: extend the grant's approved lifetime.
        db.execute('UPDATE grants SET expires_at=? WHERE id=?', (end, gid))
        return record(db, 'license.reauthorize', jid, 'reauthorized_same_right',
                      {'grant': gid, 'entitlement': same['id'], 'until': end})
    return record(db, 'license.reauthorize', jid, 'reauthorization_required',
                  {'grant': gid, 'at': at})


# --------------------------------------------------------------------------
# Return: the seat stays occupied until the vendor confirms release or the
# timeout lawfully expires it. Local deletion is never a release.
# --------------------------------------------------------------------------

def checkin(db, request):
    jid, generation, at = request['id'], request['generation'], request['at']
    gid = request.get('grant') or _grant_id(jid, generation)
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if not grant:
        return record(db, 'license.checkin', jid, 'no_grant', {'grant': gid})
    if grant['status'] in TERMINAL:
        return record(db, 'license.checkin', jid, 'idempotent',
                      {'grant': gid, 'status': grant['status']})
    if grant['status'] == QUARANTINED:
        return record(db, 'license.checkin', jid, 'quarantined_pending_verification',
                      {'grant': gid})
    gateway = get_gateway(db)
    req_id = f'return-{grant["token"]}'
    try:
        receipt = gateway.release(grant['token'], req_id, at)
    except ServiceUnavailable:
        db.execute('UPDATE grants SET status=?, return_until=? WHERE id=?',
                   (RETURN_PENDING, request.get('timeout_at', at + 300), gid))
        return record(db, 'license.checkin', jid, 'return_pending',
                      {'grant': gid, 'token': grant['token'],
                       'timeout_at': request.get('timeout_at', at + 300)})
    _store_receipt(db, receipt, at)
    if receipt['status'] in ('released', 'already_released'):
        db.execute('UPDATE grants SET status=?, released_at=? WHERE id=?',
                   (RETURNED, at, gid))
        return record(db, 'license.checkin', jid, 'returned',
                      {'grant': gid, 'token': grant['token'],
                       'receipt_id': receipt['receipt_id']})
    return record(db, 'license.checkin', jid, 'vendor_rejected_release',
                  {'grant': gid, 'status': receipt['status']})


def reconcile_return(db, request):
    """Resolve an open return-pending / quarantined seat against the vendor."""
    jid, generation, at = request['id'], request['generation'], request['at']
    gid = request.get('grant') or _grant_id(jid, generation)
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if not grant:
        return record(db, 'license.reconcile', jid, 'no_grant', {'grant': gid})
    if grant['status'] in TERMINAL:
        return record(db, 'license.reconcile', jid, 'idempotent',
                      {'grant': gid, 'status': grant['status']})
    gateway = get_gateway(db)
    # First consume any delivery-lost vendor receipts.
    try:
        pending = gateway.pending_receipts(grant['entitlement'])
    except ServiceUnavailable:
        pending = None
    if pending:
        for rcp in pending:
            rcp = dict(rcp)
            rcp['grant'] = gid
            rcp['entitlement'] = grant['entitlement']
            if rcp.get('token') == grant['token']:
                _store_receipt(db, rcp, at)
                db.execute('UPDATE grants SET status=?, released_at=? WHERE id=?',
                           (RETURNED, at, gid))
                return record(db, 'license.reconcile', jid, 'reclaimed_by_vendor',
                              {'grant': gid, 'receipt_id': rcp['receipt_id']})
    if grant['token'] is None:
        # Borrow response was lost; ask the vendor about the request token.
        return record(db, 'license.reconcile', jid, 'still_unverified',
                      {'grant': gid, 'status': grant['status']})
    try:
        verdict = gateway.verify(grant['entitlement'], grant['token'])
    except ServiceUnavailable:
        return record(db, 'license.reconcile', jid, 'service_unavailable',
                      {'grant': gid, 'status': grant['status']})
    if verdict['status'] == 'released':
        rcp = {'receipt_id': f'verify-{grant["token"]}', 'op': 'verify',
               'status': 'released', 'grant': gid,
               'entitlement': grant['entitlement'], 'token': grant['token']}
        _store_receipt(db, rcp, at)
        db.execute('UPDATE grants SET status=?, released_at=? WHERE id=?',
                   (RETURNED, at, gid))
        return record(db, 'license.reconcile', jid, 'verified_returned',
                      {'grant': gid, 'receipt_id': rcp['receipt_id']})
    return record(db, 'license.reconcile', jid, 'still_occupied',
                  {'grant': gid, 'vendor_status': verdict['status']})


def timeout_return(db, request):
    """Lawful expiry of a return the vendor never confirmed.

    The original record is preserved and an immutable correction is appended;
    quarantined seats whose occupancy is unknown are never silently freed.
    """
    jid, generation, at = request['id'], request['generation'], request['at']
    gid = request.get('grant') or _grant_id(jid, generation)
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if not grant:
        return record(db, 'license.timeout', jid, 'no_grant', {'grant': gid})
    if grant['status'] in TERMINAL:
        return record(db, 'license.timeout', jid, 'idempotent',
                      {'grant': gid, 'status': grant['status']})
    if grant['status'] == QUARANTINED:
        return record(db, 'license.timeout', jid, 'cannot_timeout_quarantine',
                      {'grant': gid})
    if grant['status'] != RETURN_PENDING:
        return record(db, 'license.timeout', jid, 'not_return_pending',
                      {'grant': gid, 'status': grant['status']})
    if grant['return_until'] is not None and at < grant['return_until']:
        return record(db, 'license.timeout', jid, 'timeout_not_reached',
                      {'return_until': grant['return_until'], 'at': at})
    db.execute('UPDATE grants SET status=? WHERE id=?', (TIMED_OUT, gid))
    _store_correction(db, f'timeout-{gid}', 'return_timeout', gid,
                      grant['entitlement'], grant['token'], at,
                      {'request_id': grant['request_id'],
                       'return_until': grant['return_until']})
    return record(db, 'license.timeout', jid, 'timed_out',
                  {'grant': gid, 'token': grant['token']})


# --------------------------------------------------------------------------
# Late vendor receipts and post-hoc corrections
# --------------------------------------------------------------------------

def receive_receipt(db, request):
    """A receipt that arrives late (network or vendor replay).

    A late success can never re-legalise an expired, cancelled or
    old-generation attempt; it is stored marked stale and only closes a
    genuinely matching open return.
    """
    at = request['at']
    payload = dict(request['receipt'])
    token = payload.get('token')
    grant = row(db, 'SELECT * FROM grants WHERE token=?', (token,)) if token else None
    payload['grant'] = grant['id'] if grant else None
    payload.setdefault('entitlement', grant['entitlement'] if grant else None)
    stale = False
    decision = 'receipt_recorded'
    if grant is None:
        stale, decision = True, 'orphan_receipt_quarantined'
    elif grant['remote_cancelled']:
        stale, decision = True, 'receipt_for_cancelled_entitlement'
    elif payload.get('generation') is not None and payload['generation'] != grant['generation']:
        stale, decision = True, 'stale_generation_rejected'
    elif grant['status'] in (CANCELLED, TIMED_OUT):
        stale, decision = True, f'late_receipt_for_{grant["status"]}'
    elif payload['op'] == 'borrow' and payload.get('status') == 'granted' and grant['status'] not in (HELD, EMERGENCY):
        stale, decision = True, 'late_borrow_cannot_relegalize'
    elif payload['op'] == 'release' and payload.get('status') in ('released', 'vendor_reclaimed') and grant['status'] == RETURN_PENDING:
        _store_receipt(db, payload, at)
        db.execute('UPDATE grants SET status=?, released_at=? WHERE id=?',
                   (RETURNED, at, grant['id']))
        return record(db, 'license.receipt', grant['job'], 'late_return_confirmed',
                      {'grant': grant['id'], 'receipt_id': payload['receipt_id']})
    _store_receipt(db, payload, at, stale=stale)
    return record(db, 'license.receipt',
                  grant['job'] if grant else (payload.get('job') or 'unknown'),
                  decision, {'receipt_id': payload.get('receipt_id'),
                             'stale': stale})


def vendor_correction(db, request):
    """Vendor post-hoc correction (double-charge refund, error, etc.).

    Original receipts stay untouched; the correction is immutable and linked
    to the grant and the vendor document it references.
    """
    at, payload = request['at'], dict(request['correction'])
    cid = payload.get('correction_id') or request.get('correction_id')
    if not cid:
        return record(db, 'license.correction', 'unknown', 'invalid_correction', {})
    if row(db, 'SELECT 1 FROM license_corrections WHERE correction_id=?', (cid,)):
        return record(db, 'license.correction', 'unknown', 'idempotent',
                      {'correction_id': cid})
    token = payload.get('token')
    grant = row(db, 'SELECT * FROM grants WHERE token=?', (token,)) if token else None
    kind = payload.get('kind', 'vendor_adjustment')
    gid = grant['id'] if grant else None
    eid = grant['entitlement'] if grant else payload.get('entitlement')
    _store_correction(db, cid, kind, gid, eid,
                      payload.get('reference', payload.get('receipt_id', '')),
                      at, payload)
    if kind == 'double_charge_refund' and grant is not None:
        # Refund is a correction record; the grant's own lifecycle is not
        # rewritten. If it still showed a live hold, mark it quarantined for
        # manual write-off rather than silently freeing it.
        if grant['status'] in (HELD, RETURN_PENDING):
            db.execute('UPDATE grants SET status=? WHERE id=?', (QUARANTINED, gid))
            return record(db, 'license.correction', grant['job'],
                          'double_charge_quarantined',
                          {'grant': gid, 'correction_id': cid})
    return record(db, 'license.correction',
                  grant['job'] if grant else (payload.get('job') or 'unknown'),
                  'correction_recorded',
                  {'grant': gid, 'correction_id': cid, 'kind': kind})


def write_off(db, request):
    """Manual, audited disposal of a quarantined seat."""
    at = request['at']
    gid = request.get('grant') or _grant_id(request['id'], request['generation'])
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (gid,))
    if not grant:
        return record(db, 'license.writeoff', request['id'], 'no_grant', {'grant': gid})
    if grant['status'] != QUARANTINED:
        return record(db, 'license.writeoff', request['id'], 'not_quarantined',
                      {'grant': gid, 'status': grant['status']})
    db.execute('UPDATE grants SET status=?, released_at=? WHERE id=?',
               (CANCELLED, at, gid))
    _store_correction(db, request['correction_id'], 'manual_write_off', gid,
                      grant['entitlement'], grant['token'] or grant['request_id'],
                      at, {'reason': request.get('reason', ''),
                           'operator': request.get('operator', 'unknown')})
    return record(db, 'license.writeoff', request['id'], 'written_off',
                  {'grant': gid, 'correction_id': request['correction_id']})


# --------------------------------------------------------------------------
# Delivery gating and compliance audit
# --------------------------------------------------------------------------

def active_grant(db, jid, generation):
    grant = row(db, 'SELECT * FROM grants WHERE id=?', (_grant_id(jid, generation),))
    if not grant or grant['status'] not in (HELD, EMERGENCY):
        return None
    return grant


def attach_delivery_evidence(db, jid, generation, at):
    """Called from the delivery boundary; returns evidence or None."""
    grant = active_grant(db, jid, generation)
    if grant is None:
        return None
    rcp = row(db, "SELECT * FROM license_receipts WHERE token=? AND op='borrow' AND stale=0 ORDER BY seq LIMIT 1",
              (grant['token'],))
    receipt_id = rcp['receipt_id'] if rcp else f'emergency-{grant["id"]}'
    db.execute('INSERT OR REPLACE INTO delivery_evidence VALUES(?,?,?,?,?)',
               (jid, grant['id'], grant['token'], receipt_id, at))
    return {'grant': grant['id'], 'token': grant['token'],
            'receipt_id': receipt_id,
            'responsibility': grant['responsibility'],
            'entitlement': grant['entitlement']}


def release_for_job(db, jid, at, timeout_at=None):
    """Best-effort seat release on cancellation/failure/terminal delivery."""
    out = []
    for grant in rows(db, "SELECT * FROM grants WHERE job=? AND status IN ('held','emergency')", (jid,)):
        out.append(checkin(db, {'id': jid, 'generation': grant['generation'],
                                'at': at, 'grant': grant['id'],
                                'timeout_at': timeout_at or at + 300}))
    return out


def compliance_report(db, jid):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not job:
        return record(db, 'license.audit', jid, 'missing', {})
    need = row(db, 'SELECT * FROM job_license WHERE job=?', (jid,))
    grants = rows(db, 'SELECT * FROM grants WHERE job=? ORDER BY generation,id', (jid,))
    evidence = row(db, 'SELECT * FROM delivery_evidence WHERE job=?', (jid,))
    receipts = rows(db, 'SELECT seq,receipt_id,op,token,status,stale,received_at FROM license_receipts WHERE grant IN (SELECT id FROM grants WHERE job=?) OR entitlement IN (SELECT entitlement FROM grants WHERE job=?) ORDER BY seq',
                    (jid, jid))
    corrections = rows(db, 'SELECT * FROM license_corrections WHERE grant IN (SELECT id FROM grants WHERE job=?) OR entitlement IN (SELECT entitlement FROM grants WHERE job=?) ORDER BY seq',
                       (jid, jid))
    if not need:
        decision = 'no_license_required'
    elif not grants:
        decision = 'no_grant'
    elif any(g['status'] == QUARANTINED for g in grants):
        decision = 'quarantined_seats'
    elif evidence is None:
        decision = 'delivery_without_license_evidence' if job['status'] == 'delivered' else 'no_delivery_evidence'
    else:
        decision = 'compliant'
    return record(db, 'license.audit', jid, decision,
                  {'requirement': dict(need) if need else None,
                   'grants': grants, 'delivery_evidence': dict(evidence) if evidence else None,
                   'receipts': receipts, 'corrections': corrections,
                   'responsibility': evidence and next((g['responsibility'] for g in grants if g['id'] == evidence['grant']), None)})
