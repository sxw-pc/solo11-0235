from .store import row,rows,record

def inspect(db,jid):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if not job:return record(db,'audit',jid,'missing',{})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    artifact=row(db,'SELECT * FROM artifacts WHERE job=?',(jid,))
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    attempt=row(db,'SELECT * FROM attempts WHERE job=? ORDER BY generation DESC LIMIT 1',(jid,))
    if job['status']=='cancelled':decision='cancelled'
    elif artifact and artifact['status']=='delivered' and not unmet:decision='delivered'
    elif unmet:decision='waiting_parents'
    elif job['status']=='breached':decision='breached'
    elif attempt and attempt['status']=='failed':decision='retry_needed'
    elif lease and lease['status']=='active':decision='running_or_reserved'
    else:decision='awaiting_slot'
    claim_summary=None
    if row(db,"SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"):
        claim_row=row(db,'SELECT id,status,liability,remedy_kind FROM claims WHERE job=? ORDER BY created_at DESC LIMIT 1',(jid,))
        if claim_row:claim_summary={'id':claim_row['id'],'status':claim_row['status'],'liability':claim_row['liability'],'remedy':claim_row['remedy_kind']}
    result=record(db,'audit',jid,decision,{'job_version':job['version'],'parents_unmet':unmet,'lease_generation':lease['generation'] if lease else None,'attempt_generation':attempt['generation'] if attempt else None,'stored_status':job['status'],'claim':claim_summary})
    result['claim']=claim_summary
    return result
