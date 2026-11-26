from .store import row,rows,record
from . import secret_rotation as sr

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
    secret={}
    if row(db,"SELECT 1 FROM secret_envelope_versions WHERE job=?",(jid,)):
        sr._ensure_tables(db)
        verdict=sr.assess_delivery(db,jid,0)
        secret={
            'envelope_versions':rows(db,'SELECT key_id,version,status FROM secret_envelope_versions WHERE job=? ORDER BY version',(jid,)),
            'active_mount':row(db,'SELECT mount_id,key_id,version,lease_generation,slot,expires_at,status FROM secret_mounts WHERE job=? AND status=?',(jid,'active')),
            'revocations':rows(db,'SELECT id,key_id,version,reason,status FROM secret_revocations WHERE key_id IN (SELECT DISTINCT key_id FROM secret_envelope_versions WHERE job=?)',(jid,)),
            'kms_receipts':rows(db,'SELECT receipt_id,key_id,version,action,duplicated FROM kms_revocations WHERE key_id IN (SELECT DISTINCT key_id FROM secret_envelope_versions WHERE job=?)',(jid,)),
            'delivery_eligible':verdict['eligible'],
            'tainted_versions':verdict['tainted_versions'],
            'recovery_points':rows(db,'SELECT recovery_id,lease_generation,key_id,version,tainted FROM recovery_points WHERE job=?',(jid,)),
        }
    return record(db,'audit',jid,decision,{'job_version':job['version'],'parents_unmet':unmet,'lease_generation':lease['generation'] if lease else None,'attempt_generation':attempt['generation'] if attempt else None,'stored_status':job['status'],'secret':secret})
