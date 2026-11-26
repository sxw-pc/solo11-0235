"""Data residency and processing-permission enforcement.

Licensing here is *effective*, not advisory: the same approved input manifest
and license versions gate lease placement (physical node), remote staging and
recovery points (temporary copies), de-identification (real new bytes plus a
verifiable transformation proof), cross-border egress and final delivery.
Every decision is written to the append-only ``events`` log and every
unauthorized processing location is also pinned in ``rviolations`` as an
immutable record that survives later deletion of the offending bytes and
later issuance of a broader license.
"""
import hashlib
import json

from .store import row, rows, record


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _loads(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest_of(data):
    """Content digest over the canonical encoding of text or structured data."""
    if isinstance(data, str):
        raw = data.encode()
    else:
        raw = _canonical(data).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def signature_of(dataset, version, scope, signer):
    """Deterministic stand-in for an asymmetric license signature.

    A signed row only becomes effective when every required signer is present,
    so a late-arriving signature cannot be back-dated and an unsigned license
    never authorizes anything.
    """
    return 'sig:' + hashlib.sha256(
        ('|'.join((dataset, version, signer, _canonical(scope)))).encode()
    ).hexdigest()[:24]


def _manifest_digest(entries):
    return digest_of(sorted((e['dataset'], e['version']) for e in entries))


def _scope_of(license_row):
    return _loads(license_row['scope'], {})


def _licenses_for(db, manifest):
    out = []
    for entry in rows(
        db,
        'SELECT * FROM rmanifest_entries WHERE manifest=? ORDER BY dataset',
        (manifest,),
    ):
        lic = row(
            db,
            'SELECT * FROM rlicenses WHERE dataset=? AND version=?',
            (entry['dataset'], entry['license_version']),
        )
        out.append((entry, lic))
    return out


def _intersection(db, manifest):
    """Intersection over every input. A narrow parent license can never be
    widened by another parent's broad permission."""
    locations = None
    node_categories = None
    egress = None
    deletion_days = None
    for _entry, lic in _licenses_for(db, manifest):
        scope = _scope_of(lic)
        locs = set(scope.get('processing_locations', ()))
        if locations is None:
            locations = locs
        else:
            locations &= locs
        cats = set(scope.get('node_categories', ()))
        if node_categories is None:
            node_categories = cats
        else:
            node_categories &= cats
        rules = scope.get('egress', [])
        egress = rules if egress is None else egress + rules
        days = scope.get('deletion_within_days')
        if days is not None:
            deletion_days = days if deletion_days is None else min(deletion_days, days)
    return {
        'processing_locations': sorted(locations or ()),
        'node_categories': sorted(node_categories or ()),
        'egress': egress or [],
        'deletion_within_days': deletion_days,
    }


def _license_violations(db, manifest):
    blocked = []
    for entry, lic in _licenses_for(db, manifest):
        if lic is None:
            blocked.append({'dataset': entry['dataset'], 'reason': 'unknown_license'})
        elif lic['status'] == 'revoked':
            blocked.append({'dataset': entry['dataset'], 'reason': 'license_revoked',
                            'revoked_at': lic['revoked_at']})
        elif not lic['signed']:
            blocked.append({'dataset': entry['dataset'], 'reason': 'license_not_signed',
                            'version': lic['version']})
    return blocked


def _resolve_node(db, location):
    """A processing location may be named either as a physical node or as the
    execution slot hosted on that node; both resolve to the same rnode."""
    node = row(db, 'SELECT * FROM rnodes WHERE node=?', (location,))
    if node is not None:
        return node, None
    slot = row(db, 'SELECT node FROM slots WHERE id=?', (location,))
    if slot is not None:
        node = row(db, 'SELECT * FROM rnodes WHERE node=?', (slot['node'],))
        if node is not None:
            return node, None
        return None, 'unregistered_jurisdiction'
    return None, 'unknown_location'


def _node_row(db, location):
    return _resolve_node(db, location)


# ---------------------------------------------------------------------------
# catalog: datasets, nodes, licenses
# ---------------------------------------------------------------------------

def register_dataset(db, request):
    did = request['id']
    fields = sorted(request.get('fields', ()))
    digest = request.get('digest') or digest_of(request.get('content', ''))
    old = row(db, 'SELECT * FROM rdatasets WHERE id=?', (did,))
    if old:
        return record(db, 'residency', did, 'dataset_conflict',
                      {'existing_digest': old['digest']})
    db.execute(
        'INSERT INTO rdatasets VALUES(?,?,?,?,?,?,?)',
        (did, request['origin'], request['category'], _canonical(fields),
         digest, request.get('producer_job'), request['at']),
    )
    return record(db, 'residency', did, 'dataset_registered',
                  {'origin': request['origin'], 'category': request['category'],
                   'fields': fields, 'digest': digest})


def register_node(db, request):
    node = request['node']
    categories = sorted(request.get('categories', ()))
    db.execute(
        'INSERT OR REPLACE INTO rnodes VALUES(?,?,?,?)',
        (node, request['jurisdiction'], int(request.get('controlled', False)),
         _canonical(categories)),
    )
    return record(db, 'residency', node, 'node_registered',
                  {'jurisdiction': request['jurisdiction'],
                   'controlled': bool(request.get('controlled', False)),
                   'categories': categories})


def issue_license(db, request):
    did, version = request['dataset'], request['version']
    if not row(db, 'SELECT id FROM rdatasets WHERE id=?', (did,)):
        return record(db, 'residency', did, 'unknown_dataset', {'version': version})
    scope = request['scope']
    required = sorted(scope.get('required_signers', ()))
    signers = sorted(request.get('signers', ()))
    valid = sorted(
        s for s in signers
        if request.get('signatures', {}).get(s) == signature_of(did, version, scope, s)
    )
    signed = bool(required) and set(required) <= set(valid)
    if row(db, 'SELECT * FROM rlicenses WHERE dataset=? AND version=?', (did, version)):
        return record(db, 'residency', did, 'license_conflict', {'version': version})
    db.execute(
        'INSERT INTO rlicenses VALUES(?,?,?,?,?,?,?,?,?,?)',
        (did, version, _canonical(scope), 'granted' if signed else 'pending',
         int(signed), _canonical(signers), _canonical(required),
         request['at'] if signed else None, None, None),
    )
    return record(db, 'residency', did,
                  'license_granted' if signed else 'license_pending_signature',
                  {'version': version, 'required_signers': required,
                   'valid_signers': valid})


def sign_license(db, request):
    did, version, signer = request['dataset'], request['version'], request['signer']
    lic = row(db, 'SELECT * FROM rlicenses WHERE dataset=? AND version=?', (did, version))
    if lic is None:
        return record(db, 'residency', did, 'unknown_license', {'version': version})
    if lic['status'] == 'revoked':
        return record(db, 'residency', did, 'license_revoked', {'version': version})
    scope = _scope_of(lic)
    if request.get('signature') != signature_of(did, version, scope, signer):
        return record(db, 'residency', did, 'bad_signature',
                      {'version': version, 'signer': signer})
    signers = set(_loads(lic['signers'], []))
    signers.add(signer)
    required = set(_loads(lic['required_signers'], []))
    signed = required <= signers
    db.execute(
        'UPDATE rlicenses SET signers=?, signed=?, status=?, granted_at=COALESCE(granted_at,?) '
        'WHERE dataset=? AND version=?',
        (_canonical(sorted(signers)), int(signed),
         'granted' if signed else 'pending', request['at'], did, version),
    )
    return record(db, 'residency', did,
                  'license_granted' if signed else 'signature_accepted',
                  {'version': version, 'signers': sorted(signers)})


def revoke_license(db, request):
    did, version = request['dataset'], request['version']
    lic = row(db, 'SELECT * FROM rlicenses WHERE dataset=? AND version=?', (did, version))
    if lic is None:
        return record(db, 'residency', did, 'unknown_license', {'version': version})
    if lic['status'] == 'revoked':
        return record(db, 'residency', did, 'revocation_idempotent', {'version': version})
    delivered_before = rows(
        db,
        'SELECT job, generation FROM regress '
        'WHERE manifest IN (SELECT manifest FROM rmanifest_entries '
        'WHERE dataset=? AND license_version=?)',
        (did, version),
    )
    db.execute(
        'UPDATE rlicenses SET status=?, revoked_at=?, revoke_reason=? '
        'WHERE dataset=? AND version=?',
        ('revoked', request['at'], request.get('reason', ''), did, version),
    )
    # revocation only stops *future* processing and redistribution: active
    # leases pinned to the revoked version are released, delivered artifacts
    # keep their point-in-time license evidence but inherit deletion duties.
    pinned_jobs = rows(
        db,
        'SELECT DISTINCT rm.job FROM rjob_manifest rm '
        'JOIN rmanifest_entries me ON me.manifest=rm.manifest '
        'WHERE me.dataset=? AND me.license_version=?',
        (did, version),
    )
    stopped = []
    for item in pinned_jobs:
        jid = item['job']
        db.execute(
            "UPDATE leases SET status='released' WHERE job=? AND status='active'",
            (jid,),
        )
        stopped.append(jid)
    duties = []
    for item in delivered_before:
        due = request['at'] + int(request.get('deletion_within_days', 30))
        db.execute(
            'INSERT INTO rdeletion_duties(job,dataset,due,reason,status,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (item['job'], did, due, 'license_revoked', 'open', request['at']),
        )
        duties.append({'job': item['job'], 'due': due})
    return record(db, 'residency', did, 'license_revoked',
                  {'version': version, 'at': request['at'],
                   'active_leases_released': stopped,
                   'delivered_evidence_preserved': [x['job'] for x in delivered_before],
                   'deletion_duties': duties})


# ---------------------------------------------------------------------------
# approved input manifest: intersection of every input's pinned license
# ---------------------------------------------------------------------------

def approve_manifest(db, request):
    jid, manifest_id = request['job'], request['id']
    entries = request['entries']
    if not entries:
        return record(db, 'residency', jid, 'manifest_empty', {})
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if job is None:
        return record(db, 'residency', jid, 'unknown_job', {})
    blocked = []
    pinned = []
    for entry in entries:
        did = entry['dataset']
        ds = row(db, 'SELECT * FROM rdatasets WHERE id=?', (did,))
        if ds is None:
            blocked.append({'dataset': did, 'reason': 'unknown_dataset'})
            continue
        lic = row(
            db,
            'SELECT * FROM rlicenses WHERE dataset=? AND version=?',
            (did, entry['version']),
        )
        if lic is None:
            blocked.append({'dataset': did, 'reason': 'unknown_license'})
        elif lic['status'] == 'revoked':
            blocked.append({'dataset': did, 'reason': 'license_revoked',
                            'revoked_at': lic['revoked_at']})
        elif not lic['signed']:
            blocked.append({'dataset': did, 'reason': 'license_not_signed',
                            'version': lic['version']})
        pinned.append({'dataset': did, 'version': entry['version'],
                       'dataset_digest': ds['digest'], 'category': ds['category'],
                       'origin': ds['origin']})
    if blocked:
        # the refusal itself is durable evidence; no manifest becomes usable
        return record(db, 'residency', jid, 'manifest_rejected',
                      {'manifest': manifest_id, 'blocked': blocked})
    digest = _manifest_digest(entries)
    existing = row(db, 'SELECT * FROM rmanifests WHERE id=?', (manifest_id,))
    if existing:
        if existing['digest'] != digest or existing['status'] != 'approved':
            return record(db, 'residency', jid, 'manifest_conflict',
                          {'manifest': manifest_id})
    else:
        db.execute('INSERT INTO rmanifests VALUES(?,?,?,?,?)',
                   (manifest_id, jid, digest, 'approved', request['at']))
        for item in pinned:
            db.execute('INSERT OR REPLACE INTO rmanifest_entries VALUES(?,?,?,?)',
                       (manifest_id, item['dataset'], item['version'],
                        item['dataset_digest']))
    db.execute('INSERT OR REPLACE INTO rjob_manifest VALUES(?,?)', (jid, manifest_id))
    effective = _intersection(db, manifest_id)
    decision = 'manifest_approved'
    if not effective['processing_locations']:
        decision = 'manifest_conflicting_restrictions'
    return record(db, 'residency', jid, decision,
                  {'manifest': manifest_id, 'digest': digest,
                   'inputs': sorted((p['dataset'], p['version']) for p in pinned),
                   'effective_permissions': effective})


def job_manifest(db, jid):
    link = row(db, 'SELECT * FROM rjob_manifest WHERE job=?', (jid,))
    if link is None:
        return None, None
    manifest = row(db, 'SELECT * FROM rmanifests WHERE id=?', (link['manifest'],))
    return link, manifest


# ---------------------------------------------------------------------------
# gates reused by slots / runs
# ---------------------------------------------------------------------------

def lease_gate(db, jid, location, generation=None):
    """Placement decision for a physical node (slot node, staging host or
    recovery-point host). Returns (ok, decision, evidence)."""
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return True, 'ungoverned', {}
    if manifest['status'] != 'approved':
        return False, 'residency_manifest_not_approved', {'manifest': manifest['id']}
    blocked = _license_violations(db, manifest['id'])
    if blocked:
        return False, 'residency_license_blocked', {'manifest': manifest['id'],
                                                    'blocked': blocked}
    node, err = _node_row(db, location)
    if err:
        return False, 'residency_' + err, {'location': location}
    effective = _intersection(db, manifest['id'])
    if node['jurisdiction'] not in effective['processing_locations']:
        return False, 'residency_jurisdiction_denied', {
            'location': location, 'jurisdiction': node['jurisdiction'],
            'allowed': effective['processing_locations'],
            'manifest': manifest['id'], 'manifest_digest': manifest['digest']}
    node_categories = set(_loads(node['categories'], []))
    if node_categories and not (set(effective['node_categories']) & node_categories):
        return False, 'residency_node_category_denied', {
            'location': location, 'node_categories': sorted(node_categories),
            'required_any': effective['node_categories']}
    return True, 'residency_allowed', {
        'location': location, 'jurisdiction': node['jurisdiction'],
        'manifest': manifest['id'], 'manifest_digest': manifest['digest'],
        'effective_permissions': effective,
        'controlled': bool(node['controlled'])}


def start_gate(db, jid, generation):
    """Re-verification at every (re)start: same manifest and license versions,
    same physical location, licenses still effective."""
    link, manifest = job_manifest(db, jid)
    if manifest is None:
        return True, 'ungoverned', {}
    if manifest['status'] != 'approved':
        return False, 'residency_manifest_not_approved', {'manifest': manifest['id']}
    # the basis pinned when this generation first started can never be swapped,
    # even if a newer manifest has since replaced it on the job
    if generation is not None:
        seen = row(db, 'SELECT * FROM rattempts WHERE job=? AND generation=?',
                   (jid, generation))
        if seen is not None and seen['manifest'] != manifest['id']:
            return False, 'residency_manifest_generation_mismatch', {
                'expected': seen['manifest'], 'current': manifest['id']}
    blocked = _license_violations(db, manifest['id'])
    if blocked:
        return False, 'residency_license_blocked', {'manifest': manifest['id'],
                                                    'blocked': blocked}
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    if lease is None:
        return False, 'residency_not_leased', {}
    ok, decision, evidence = lease_gate(db, jid, lease['slot'], generation)
    if not ok:
        return False, decision, evidence
    if generation is not None and seen is None:
        db.execute('INSERT OR REPLACE INTO rattempts VALUES(?,?,?)',
                   (jid, generation, manifest['id']))
    return True, 'residency_start_verified', evidence


def _violation(db, jid, generation, manifest_id, dataset, location, jurisdiction,
               kind, reason, at, extra=None):
    vid = 'viol-%s' % hashlib.sha256(_canonical(
        [jid, generation, dataset, location, kind, reason, at]).encode()).hexdigest()[:16]
    evidence = {'job': jid, 'generation': generation, 'dataset': dataset,
                'location': location, 'jurisdiction': jurisdiction, 'kind': kind,
                'reason': reason, 'at': at, 'manifest': manifest_id}
    if extra:
        evidence.update(extra)
    db.execute(
        'INSERT OR IGNORE INTO rviolations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (vid, jid, generation, dataset, location, jurisdiction, kind, reason, at,
         manifest_id, _canonical(evidence)),
    )
    record(db, 'residency', jid, 'violation_recorded', evidence)
    return vid


def has_violation(db, jid):
    return bool(row(db, 'SELECT 1 FROM rviolations WHERE job=? LIMIT 1', (jid,)))


def delivery_gate(db, jid, generation):
    """Final gate before an artifact may be marked delivered."""
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return True, 'ungoverned', {}
    blocked = _license_violations(db, manifest['id'])
    if blocked:
        return False, 'residency_license_blocked', {'blocked': blocked}
    violations = rows(db, 'SELECT id,reason,location,jurisdiction FROM rviolations WHERE job=?',
                      (jid,))
    if violations:
        # an illegal temporary copy cannot be laundered by deleting it or by a
        # later, broader license: the immutable evidence blocks delivery
        return False, 'residency_violation_present', {'violations': violations}
    pinned = row(db, 'SELECT * FROM rattempts WHERE job=? AND generation=?',
                 (jid, generation))
    if pinned is None or pinned['manifest'] != manifest['id']:
        return False, 'residency_stale_generation_receipt', {
            'generation': generation, 'manifest': manifest['id']}
    unauthorized = rows(
        db,
        'SELECT id,location,jurisdiction,dataset,kind FROM rcopies '
        'WHERE job=? AND authorized=0', (jid,))
    if unauthorized:
        return False, 'residency_unauthorized_copy', {'copies': unauthorized}
    return True, 'residency_delivery_verified', {
        'manifest': manifest['id'], 'manifest_digest': manifest['digest']}


# ---------------------------------------------------------------------------
# temporary bytes: staging, recovery points and their deletion duties
# ---------------------------------------------------------------------------

def register_copy(db, request):
    jid, generation = request['job'], request['generation']
    location, dataset = request['location'], request['dataset']
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return record(db, 'residency', jid, 'copy_no_manifest',
                      {'location': location, 'dataset': dataset})
    entry = row(db, 'SELECT * FROM rmanifest_entries WHERE manifest=? AND dataset=?',
                (manifest['id'], dataset))
    if entry is None:
        return record(db, 'residency', jid, 'copy_dataset_not_in_manifest',
                      {'dataset': dataset, 'manifest': manifest['id']})
    lic = row(db, 'SELECT * FROM rlicenses WHERE dataset=? AND version=?',
              (dataset, entry['license_version']))
    if lic is None or lic['status'] == 'revoked' or not lic['signed']:
        return record(db, 'residency', jid, 'residency_license_blocked',
                      {'dataset': dataset, 'version': entry['license_version']})
    ds = row(db, 'SELECT * FROM rdatasets WHERE id=?', (dataset,))
    node, err = _node_row(db, location)
    scope = _scope_of(lic)
    fields = sorted(request.get('fields') or _loads(ds['fields'], []))
    allowed_fields = set(scope.get('allowed_fields', _loads(ds['fields'], [])))
    cur = db.execute(
        'INSERT INTO rcopies(job,generation,dataset,location,jurisdiction,kind,digest,'
        'fields,authorized,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (jid, generation, dataset, location,
         node['jurisdiction'] if node else None, request['kind'],
         request.get('digest'), _canonical(fields), 0, 'present', request['at']))
    copy_id = cur.lastrowid
    reason = None
    if err:
        reason = err
    elif node['jurisdiction'] not in set(scope.get('processing_locations', ())):
        reason = 'jurisdiction_denied'
    elif request['kind'] in ('staging', 'recovery') and \
            scope.get('controlled_environment_required', False) and not node['controlled']:
        # a license that mandates a controlled environment cannot place bytes
        # on an ordinary multi-tenant node
        reason = 'controlled_environment_required'
    elif set(fields) - allowed_fields:
        reason = 'fields_outside_license'
    if reason:
        db.execute('UPDATE rcopies SET authorized=0, violation=? WHERE id=?',
                   (reason, copy_id))
        vid = _violation(db, jid, generation, manifest['id'], dataset, location,
                         node['jurisdiction'] if node else None, request['kind'],
                         reason, request['at'],
                         {'copy': copy_id, 'digest': request.get('digest')})
        return record(db, 'residency', jid, 'residency_copy_denied',
                      {'copy': copy_id, 'location': location, 'dataset': dataset,
                       'reason': reason, 'violation': vid})
    db.execute('UPDATE rcopies SET authorized=1 WHERE id=?', (copy_id,))
    days = scope.get('deletion_within_days')
    duty = None
    if days is not None:
        due = request['at'] + int(days)
        db.execute(
            'INSERT INTO rdeletion_duties(job,dataset,due,reason,status,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (jid, dataset, due, 'temporary_copy_deletion', 'open', request['at']))
        duty = due
    return record(db, 'residency', jid, 'copy_authorized',
                  {'copy': copy_id, 'location': location, 'jurisdiction':
                   node['jurisdiction'], 'dataset': dataset, 'kind': request['kind'],
                   'fields': fields, 'deletion_due': duty})


def delete_copy(db, request):
    copy_id = request['copy']
    item = row(db, 'SELECT * FROM rcopies WHERE id=?', (copy_id,))
    if item is None:
        return record(db, 'residency', request.get('job', copy_id), 'copy_unknown',
                      {'copy': copy_id})
    if item['status'] == 'deleted':
        return record(db, 'residency', item['job'], 'copy_delete_idempotent',
                      {'copy': copy_id})
    db.execute('UPDATE rcopies SET status=?, deleted_at=? WHERE id=?',
               ('deleted', request['at'], copy_id))
    # deleting the bytes never deletes the violation evidence
    evidence = {'copy': copy_id, 'at': request['at'],
                'violation_preserved': item['violation']}
    return record(db, 'residency', item['job'], 'copy_deleted', evidence)


def discharge_deletion_duty(db, request):
    duty_id = request['duty']
    duty = row(db, 'SELECT * FROM rdeletion_duties WHERE id=?', (duty_id,))
    if duty is None:
        return record(db, 'residency', request.get('job', duty_id), 'duty_unknown',
                      {'duty': duty_id})
    if duty['status'] == 'discharged':
        return record(db, 'residency', duty['job'] or duty['dataset'],
                      'duty_idempotent', {'duty': duty_id})
    db.execute('UPDATE rdeletion_duties SET status=?, discharged_at=? WHERE id=?',
               ('discharged', request['at'], duty_id))
    return record(db, 'residency', duty['job'] or duty['dataset'], 'duty_discharged',
                  {'duty': duty_id, 'dataset': duty['dataset']})


# ---------------------------------------------------------------------------
# de-identification: real new bytes + verifiable proof, never a digest rename
# ---------------------------------------------------------------------------

def register_transform(db, request):
    jid, generation = request['job'], request['generation']
    input_id, output_id = request['input'], request['output']
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return record(db, 'residency', jid, 'transform_no_manifest', {})
    in_ds = row(db, 'SELECT * FROM rdatasets WHERE id=?', (input_id,))
    out_ds = row(db, 'SELECT * FROM rdatasets WHERE id=?', (output_id,))
    if in_ds is None or out_ds is None:
        return record(db, 'residency', jid, 'transform_unknown_dataset',
                      {'input': input_id, 'output': output_id})
    actual = digest_of(request.get('output_content', ''))
    proof = request.get('proof', {})
    ttype = request.get('ttype', 'deidentify')
    problems = []
    if actual == in_ds['digest'] or request.get('output_digest') == in_ds['digest']:
        problems.append('output_identical_to_input')
    if request.get('output_digest') and request['output_digest'] != actual:
        problems.append('output_digest_mismatch')
    dropped = set(_loads(in_ds['fields'], [])) - set(_loads(out_ds['fields'], []))
    declared_dropped = set(proof.get('dropped_fields', ()))
    # de-identification must really remove identifying fields; a controlled
    # extract may keep them, but still has to be newly produced, proven bytes
    if ttype == 'deidentify':
        if not declared_dropped or declared_dropped - dropped:
            problems.append('proof_dropped_fields_unverifiable')
    elif declared_dropped - dropped:
        problems.append('proof_dropped_fields_unverifiable')
    if proof.get('input_digest') != in_ds['digest']:
        problems.append('proof_input_digest_mismatch')
    expected_proof = digest_of({'input_digest': in_ds['digest'],
                                'dropped_fields': sorted(declared_dropped),
                                'method': proof.get('method')})
    if proof.get('proof_digest') != expected_proof:
        problems.append('proof_digest_invalid')
    if out_ds['digest'] != actual:
        problems.append('registered_output_digest_mismatch')
    if problems:
        return record(db, 'residency', jid, 'transform_unverified',
                      {'input': input_id, 'output': output_id, 'problems': problems})
    db.execute(
        'INSERT OR REPLACE INTO rtransforms VALUES(?,?,?,?,?,?,?,?,?)',
        (jid, generation, input_id, output_id, actual, ttype,
         _canonical(proof), 1, request['at']))
    return record(db, 'residency', jid, 'transform_verified',
                  {'input': input_id, 'output': output_id,
                   'output_digest': actual, 'dropped_fields': sorted(declared_dropped)})


def verified_outputs(db, jid, generation):
    return rows(
        db,
        'SELECT * FROM rtransforms WHERE job=? AND generation=? AND verified=1',
        (jid, generation))


# ---------------------------------------------------------------------------
# cross-border egress / recipient visibility
# ---------------------------------------------------------------------------

def register_egress(db, request):
    jid, generation = request['job'], request['generation']
    recipient_jur = request['recipient_jurisdiction']
    classification = request['classification']
    digest = request.get('digest') or digest_of(request.get('content', ''))
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return record(db, 'residency', jid, 'egress_no_manifest', {})
    # only bytes backed by a registered, content-verified transform may leave
    if not digest or digest == '':
        return record(db, 'residency', jid, 'egress_empty', {})
    source = row(db, 'SELECT * FROM rtransforms WHERE job=? AND generation=? AND output_digest=?',
                 (jid, generation, digest))
    if source is None:
        # raw manifest bytes can never be egressed directly; renaming a digest
        # does not create a new artifact
        in_ds = row(db, 'SELECT digest FROM rdatasets WHERE digest=?', (digest,))
        return record(db, 'residency', jid, 'egress_unverified_bytes',
                      {'digest': digest, 'raw_input': bool(in_ds)})
    blocked = _license_violations(db, manifest['id'])
    if blocked:
        return record(db, 'residency', jid, 'residency_license_blocked',
                      {'blocked': blocked})
    effective = _intersection(db, manifest['id'])
    match = next(
        (r for r in effective['egress']
         if r.get('classification') == classification
         and recipient_jur in r.get('recipient_jurisdictions', ())),
        None,
    )
    if match is None:
        return record(db, 'residency', jid, 'egress_denied',
                      {'recipient_jurisdiction': recipient_jur,
                       'classification': classification, 'digest': digest,
                       'allowed': effective['egress']})
    if match.get('requires_verified_transform', True) and not source['verified']:
        return record(db, 'residency', jid, 'egress_transform_unverified',
                      {'digest': digest})
    db.execute(
        'INSERT INTO regres(job,generation,digest,recipient,jurisdiction,classification,'
        'authorized,manifest,at) VALUES(?,?,?,?,?,?,?,?,?)',
        (jid, generation, digest, request['recipient'], recipient_jur,
         classification, 1, manifest['id'], request['at']))
    return record(db, 'residency', jid, 'egress_authorized',
                  {'recipient': request['recipient'],
                   'recipient_jurisdiction': recipient_jur,
                   'classification': classification, 'digest': digest,
                   'transform': source['output_dataset']})


def delivery_receipt(db, jid, generation, digest, at):
    """Point-in-time license evidence attached to the delivered artifact."""
    _link, manifest = job_manifest(db, jid)
    if manifest is None:
        return None
    licenses = [
        {'dataset': e['dataset'], 'version': e['license_version'],
         'digest': e['dataset_digest']}
        for e in rows(db, 'SELECT * FROM rmanifest_entries WHERE manifest=? ORDER BY dataset',
                      (manifest['id'],))
    ]
    db.execute('INSERT OR REPLACE INTO regress VALUES(?,?,?,?,?,?)',
               (jid, generation, manifest['id'], digest, at, _canonical(licenses)))
    return {'manifest': manifest['id'], 'manifest_digest': manifest['digest'],
            'licenses': licenses}


def audit(db, jid):
    link, manifest = job_manifest(db, jid)
    if manifest is None:
        return record(db, 'residency', jid, 'ungoverned', {})
    effective = _intersection(db, manifest['id'])
    violations = rows(
        db,
        'SELECT id,dataset,location,jurisdiction,kind,reason,at FROM rviolations WHERE job=?',
        (jid,))
    copies = rows(
        db,
        'SELECT id,dataset,location,jurisdiction,kind,authorized,status,deleted_at '
        'FROM rcopies WHERE job=? ORDER BY id', (jid,))
    egress = rows(
        db,
        'SELECT recipient,jurisdiction,classification,authorized,digest,at FROM regres WHERE job=?',
        (jid,))
    duties = rows(
        db,
        'SELECT id,dataset,due,status,discharged_at FROM rdeletion_duties WHERE job=?',
        (jid,))
    receipt = row(db, 'SELECT * FROM regress WHERE job=?', (jid,))
    decision = 'non_compliant' if violations else (
        'compliant' if receipt else 'governed_pending')
    return record(db, 'residency', jid, decision, {
        'manifest': manifest['id'], 'manifest_digest': manifest['digest'],
        'manifest_status': manifest['status'],
        'effective_permissions': effective,
        'license_blocks': _license_violations(db, manifest['id']),
        'copies': copies, 'violations': violations, 'egress': egress,
        'deletion_duties': duties,
        'delivery_receipt': dict(receipt) if receipt else None})
