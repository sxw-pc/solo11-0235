import hashlib
from .store import row,rows,record

def start(db,jid,generation,at):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,)); lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    if not job or job['status']!='accepted' or not lease or lease['status']!='active' or lease['generation']!=generation:
        return record(db,'start',jid,'not_leased',{'generation':generation})
    existing=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    if existing:return record(db,'start',jid,'idempotent',{})
    db.execute('INSERT INTO attempts(job,generation,status,started) VALUES(?,?,?,?)',(jid,generation,'running',at))
    return record(db,'start',jid,'running',{'generation':generation})

def settle(db,jid,generation,at,digest,success=True,content=None):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,)); attempt=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    if not attempt or not lease or lease['generation']!=generation or lease['status']!='active':
        return record(db,'settle',jid,'stale_attempt',{'generation':generation})
    if attempt['status']!='running':return record(db,'settle',jid,'idempotent',{})
    if success and (content is None or digest != 'sha256:'+hashlib.sha256(content.encode()).hexdigest()):
        return record(db,'settle',jid,'digest_mismatch',{'generation':generation})
    db.execute('UPDATE attempts SET status=?,ended=?,digest=? WHERE job=? AND generation=?',('success' if success else 'failed',at,digest,jid,generation))
    if not success:
        decision='retryable' if at+job['duration']<job['deadline'] else 'breached'
        db.execute('UPDATE leases SET status=? WHERE job=?',('failed',jid))
        if decision=='breached':db.execute('UPDATE jobs SET status=? WHERE id=?',('breached',jid))
        return record(db,'settle',jid,decision,{'at':at,'deadline':job['deadline']})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    if unmet or at>job['deadline'] or not digest:
        return record(db,'settle',jid,'delivery_blocked',{'unmet':unmet,'late':at>job['deadline']})
    db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)',(jid,digest,at,'delivered'))
    db.execute('UPDATE jobs SET status=? WHERE id=?',('delivered',jid))
    db.execute('UPDATE leases SET status=? WHERE job=?',('released',jid))
    return record(db,'settle',jid,'delivered',{'digest':digest,'at':at})

def finalize(db,jid,generation,at):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    attempt=row(db,'SELECT * FROM attempts WHERE job=? AND generation=?',(jid,generation))
    if not job or not attempt or attempt['status']!='success':
        return record(db,'finalize',jid,'no_successful_attempt',{})
    if job['status']=='delivered':return record(db,'finalize',jid,'idempotent',{})
    parents=rows(db,'SELECT parent FROM dependencies WHERE job=?',(jid,))
    unmet=[p['parent'] for p in parents if not row(db,'SELECT job FROM artifacts WHERE job=? AND status=?',(p['parent'],'delivered'))]
    if unmet:return record(db,'finalize',jid,'waiting_parents',{'unmet':unmet})
    if attempt['ended']>job['deadline']:
        db.execute('UPDATE jobs SET status=? WHERE id=?',('breached',jid))
        return record(db,'finalize',jid,'breached',{'finished':attempt['ended']})
    db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?,?)',(jid,attempt['digest'],at,'delivered'))
    db.execute('UPDATE jobs SET status=? WHERE id=?',('delivered',jid))
    db.execute('UPDATE leases SET status=? WHERE job=?',('released',jid))
    return record(db,'finalize',jid,'delivered',{'digest':attempt['digest'],'parent_count':len(parents)})

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
