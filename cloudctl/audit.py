from .store import row,rows,record

def inspect(db,jid):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if not job:return record(db,'audit',jid,'missing',{})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    artifact=row(db,'SELECT * FROM artifacts WHERE job=?',(jid,))
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    attempt=row(db,'SELECT * FROM attempts WHERE job=? ORDER BY generation DESC LIMIT 1',(jid,))
    license_need=row(db,'SELECT * FROM job_license WHERE job=?',(jid,))
    license_grant=row(db,'SELECT * FROM grants WHERE job=? ORDER BY generation DESC,id LIMIT 1',(jid,))
    license_evidence=row(db,'SELECT * FROM delivery_evidence WHERE job=?',(jid,))
    if job['status']=='cancelled':decision='cancelled'
    elif artifact and artifact['status']=='delivered' and not unmet:decision='delivered'
    elif unmet:decision='waiting_parents'
    elif job['status']=='breached':decision='breached'
    elif attempt and attempt['status']=='failed':decision='retry_needed'
    elif lease and lease['status']=='active':decision='running_or_reserved'
    else:decision='awaiting_slot'
    return record(db,'audit',jid,decision,{'job_version':job['version'],'parents_unmet':unmet,'lease_generation':lease['generation'] if lease else None,'attempt_generation':attempt['generation'] if attempt else None,'stored_status':job['status'],'license_required':license_need is not None,'license_grant':license_grant['id'] if license_grant else None,'license_token':license_grant['token'] if license_grant else None,'license_status':license_grant['status'] if license_grant else None,'license_evidence':dict(license_evidence) if license_evidence else None})
