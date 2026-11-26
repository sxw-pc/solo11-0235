from .store import row,rows,record
from . import residency

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
    _link,manifest=residency.job_manifest(db,jid)
    residency_evidence=None
    if manifest:
        violations=rows(db,'SELECT id,reason,location,jurisdiction FROM rviolations WHERE job=?',(jid,))
        receipt=row(db,'SELECT manifest,digest,licenses FROM regress WHERE job=?',(jid,))
        residency_evidence={'manifest':manifest['id'],'manifest_digest':manifest['digest'],
                            'violations':violations,'delivery_receipt':dict(receipt) if receipt else None,
                            'compliant':not violations}
    return record(db,'audit',jid,decision,{'job_version':job['version'],'parents_unmet':unmet,'lease_generation':lease['generation'] if lease else None,'attempt_generation':attempt['generation'] if attempt else None,'stored_status':job['status'],'residency':residency_evidence})
