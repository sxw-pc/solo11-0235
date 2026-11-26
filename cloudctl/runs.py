import hashlib
import json
from .store import row,rows,record
from . import licenses


def _window_covers(db,grant,at):
    return row(db,'SELECT 1 FROM grant_windows WHERE grant=? AND start_ts<=? AND end_ts>?',(grant['id'],at,at)) is not None


def _licensed_to_run(db,jid,generation,at):
    """Return (grant, reason). A seat is required exactly when the job
    declared a software requirement. The token must belong to this attempt
    generation and still cover the current window."""
    need=row(db,'SELECT * FROM job_license WHERE job=?',(jid,))
    if need is None:return None,None
    grant=licenses.active_grant(db,jid,generation)
    if grant is None:return None,'unlicensed'
    if not _window_covers(db,grant,at):
        return None,'window_boundary_reauthorization_required'
    if grant['expires_at'] is not None and at>grant['expires_at']:
        return None,'seat_expired'
    return grant,None

def start(db,jid,generation,at):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,)); lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    if not job or job['status']!='accepted' or not lease or lease['status']!='active' or lease['generation']!=generation:
        return record(db,'start',jid,'not_leased',{'generation':generation})
    if at>lease['expires']:
        db.execute('UPDATE leases SET status=? WHERE job=?',('expired',jid))
        licenses.release_for_job(db,jid,at)
        return record(db,'start',jid,'lease_expired',{'expires':lease['expires'],'at':at})
    slot=row(db,'SELECT * FROM slots WHERE id=?',(lease['slot'],))
    if not slot or not slot['healthy'] or at-slot['heartbeat']>60:
        db.execute('UPDATE leases SET status=? WHERE job=?',('isolated',jid))
        return record(db,'start',jid,'slot_unavailable',{'slot':lease['slot'],'at':at})
    existing=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    if existing:return record(db,'start',jid,'idempotent',{})
    grant,license_reason=_licensed_to_run(db,jid,generation,at)
    if license_reason:
        return record(db,'start',jid,license_reason,{'generation':generation})
    db.execute('INSERT INTO attempts(job,generation,status,started) VALUES(?,?,?,?)',(jid,generation,'running',at))
    if grant:
        # Bind the actual remote token identity to this attempt; replay of a
        # borrow receipt for an older generation can never land here.
        db.execute('INSERT OR IGNORE INTO attempt_seats VALUES(?,?,?,?)',(jid,generation,grant['id'],grant['token']))
        db.execute('INSERT OR IGNORE INTO license_receipts(receipt_id,op,grant,entitlement,token,status,payload,received_at,stale)'
                   ' VALUES(?,?,?,?,?,?,?,?,?)',
                   (f'attach-{grant["id"]}','attach',grant['id'],grant['entitlement'],grant['token'],'attached',
                    '{"job":%s,"generation":%s}'%(json.dumps(jid),generation),at,0))
    return record(db,'start',jid,'running',{'generation':generation,'grant':grant['id'] if grant else None})

def settle(db,jid,generation,at,digest,success=True,content=None):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,)); attempt=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    if not attempt or not lease or lease['generation']!=generation or lease['status']!='active':
        return record(db,'settle',jid,'stale_attempt',{'generation':generation})
    if attempt['status']!='running':return record(db,'settle',jid,'idempotent',{})
    if at>lease['expires']:
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND generation=?',('expired',at,jid,generation))
        db.execute('UPDATE leases SET status=? WHERE job=?',('expired',jid))
        licenses.release_for_job(db,jid,at)
        return record(db,'settle',jid,'lease_expired',{'expires':lease['expires'],'at':at})
    slot=row(db,'SELECT * FROM slots WHERE id=?',(lease['slot'],))
    if not slot or not slot['healthy'] or at-slot['heartbeat']>60:
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND generation=?',('isolated',at,jid,generation))
        db.execute('UPDATE leases SET status=? WHERE job=?',('isolated',jid))
        return record(db,'settle',jid,'slot_unavailable',{'slot':lease['slot'],'at':at})
    grant,license_reason=_licensed_to_run(db,jid,generation,at)
    if license_reason:
        return record(db,'settle',jid,license_reason,{'generation':generation})
    if success and (content is None or digest != 'sha256:'+hashlib.sha256(content.encode()).hexdigest()):
        return record(db,'settle',jid,'digest_mismatch',{'generation':generation})
    db.execute('UPDATE attempts SET status=?,ended=?,digest=? WHERE job=? AND generation=?',('success' if success else 'failed',at,digest,jid,generation))
    if not success:
        decision='retryable' if at+job['duration']<job['deadline'] else 'breached'
        db.execute('UPDATE leases SET status=? WHERE job=?',('failed',jid))
        # Failed attempt ends: give the vendor token back.
        licenses.release_for_job(db,jid,at)
        if decision=='breached':db.execute('UPDATE jobs SET status=? WHERE id=?',('breached',jid))
        return record(db,'settle',jid,decision,{'at':at,'deadline':job['deadline']})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    if unmet or at>job['deadline'] or not digest:
        return record(db,'settle',jid,'delivery_blocked',{'unmet':unmet,'late':at>job['deadline']})
    evidence=licenses.attach_delivery_evidence(db,jid,generation,at)
    if evidence is None and row(db,'SELECT 1 FROM job_license WHERE job=?',(jid,)):
        return record(db,'settle',jid,'delivery_without_license_evidence',{'generation':generation})
    db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)',(jid,digest,at,'delivered'))
    db.execute('UPDATE jobs SET status=? WHERE id=?',('delivered',jid))
    db.execute('UPDATE leases SET status=? WHERE job=?',('released',jid))
    # Delivery closes the run: return the seat and prove it.
    licenses.release_for_job(db,jid,at)
    return record(db,'settle',jid,'delivered',{'digest':digest,'at':at,'license_evidence':evidence})

def finalize(db,jid,generation,at):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    attempt=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    if not job or not attempt or attempt['status']!='success':
        return record(db,'finalize',jid,'no_successful_attempt',{})
    if job['status'] not in ('accepted','delivered'):
        return record(db,'finalize',jid,'job_not_active',{'status':job['status']})
    if job['status']=='delivered':return record(db,'finalize',jid,'idempotent',{})
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    if not lease or lease['generation']!=generation or lease['status']!='active':
        return record(db,'finalize',jid,'stale_attempt',{'generation':generation})
    if at>lease['expires']:
        db.execute('UPDATE leases SET status=? WHERE job=?',('expired',jid))
        licenses.release_for_job(db,jid,at)
        return record(db,'finalize',jid,'lease_expired',{'expires':lease['expires'],'at':at})
    slot=row(db,'SELECT * FROM slots WHERE id=?',(lease['slot'],))
    if not slot or not slot['healthy'] or at-slot['heartbeat']>60:
        db.execute('UPDATE leases SET status=? WHERE job=?',('isolated',jid))
        return record(db,'finalize',jid,'slot_unavailable',{'slot':lease['slot'],'at':at})
    grant,license_reason=_licensed_to_run(db,jid,generation,at)
    if license_reason:
        return record(db,'finalize',jid,license_reason,{'generation':generation})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    if unmet:return record(db,'finalize',jid,'waiting_parents',{'unmet':unmet})
    if attempt['ended']>job['deadline']:
        db.execute('UPDATE jobs SET status=? WHERE id=?',('breached',jid))
        return record(db,'finalize',jid,'breached',{'finished':attempt['ended']})
    evidence=licenses.attach_delivery_evidence(db,jid,generation,at)
    if evidence is None and row(db,'SELECT 1 FROM job_license WHERE job=?',(jid,)):
        return record(db,'finalize',jid,'delivery_without_license_evidence',{'generation':generation})
    db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)',(jid,attempt['digest'],at,'delivered'))
    db.execute('UPDATE jobs SET status=? WHERE id=?',('delivered',jid))
    db.execute('UPDATE leases SET status=? WHERE job=?',('released',jid))
    licenses.release_for_job(db,jid,at)
    return record(db,'finalize',jid,'delivered',{'digest':attempt['digest'],'parent_count':len(parents),'license_evidence':evidence})

def retention(db,jid,expires,hold,reason):
    artifact=row(db,'SELECT * FROM artifacts WHERE job=? AND status=?',(jid,'delivered'))
    if not artifact:return record(db,'retention',jid,'not_delivered',{})
    db.execute('INSERT OR REPLACE INTO retentions VALUES(?,?,?,?)',(jid,expires,int(hold),reason))
    return record(db,'retention',jid,'held' if hold else 'retained',{'expires':expires})

def purge(db,at):
    candidates=rows(db,'SELECT job FROM retentions WHERE expires<=? AND hold=0',(at,))
    for item in candidates:
        db.execute('UPDATE artifacts SET status=? WHERE job=?',('purged',item['job']))
        record(db,'purge',item['job'],'purged',{'at':at})
    return {'decision':'purged','jobs':[x['job'] for x in candidates]}
