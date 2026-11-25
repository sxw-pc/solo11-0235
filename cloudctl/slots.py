from .store import row,rows,record

def register(db,request):
    old=row(db,'SELECT * FROM slots WHERE id=?',(request['id'],))
    if old and old['cpu']!=request['cpu']:return record(db,'slot',request['id'],'conflict',{})
    db.execute('INSERT OR REPLACE INTO slots VALUES(?,?,?,?,?)',(request['id'],request['cpu'],request['node'],1,request['at']))
    return record(db,'slot',request['id'],'ready',{'cpu':request['cpu']})

def lease(db,jid,slot_id,now,expires,preempt=False):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,)); slot=row(db,'SELECT * FROM slots WHERE id=?',(slot_id,))
    if not job or job['status']!='accepted':return record(db,'lease',jid,'job_unavailable',{})
    if not slot or not slot['healthy'] or now-slot['heartbeat']>60:
        return record(db,'lease',jid,'slot_unavailable',{})
    if job['cpu']>slot['cpu'] or expires>job['deadline']:
        return record(db,'lease',jid,'capacity_or_deadline',{})
    active=rows(db,'SELECT * FROM leases WHERE slot=? AND status=?',(slot_id,'active'))
    if active and not preempt:return record(db,'lease',jid,'occupied',{'by':[a['job'] for a in active]})
    for a in active:
        if row(db,'SELECT status FROM artifacts WHERE job=?',(a['job'],)):
            return record(db,'lease',jid,'delivered_cannot_preempt',{})
        db.execute('UPDATE leases SET status=?,debt=? WHERE job=?',('preempted',max(0,a['expires']-now),a['job']))
    generation=(row(db,'SELECT generation FROM leases WHERE job=?',(jid,)) or {}).get('generation',0)+1
    db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?)',(jid,slot_id,generation,expires,'active',0))
    return record(db,'lease',jid,'leased',{'slot':slot_id,'generation':generation,'preempted':[a['job'] for a in active]})

def heartbeat(db,slot_id,at,healthy):
    slot=row(db,'SELECT * FROM slots WHERE id=?',(slot_id,))
    if not slot:return record(db,'heartbeat',slot_id,'missing',{})
    if at<slot['heartbeat']:return record(db,'heartbeat',slot_id,'stale',{})
    db.execute('UPDATE slots SET heartbeat=?,healthy=? WHERE id=?',(at,int(healthy),slot_id))
    if not healthy:db.execute('UPDATE leases SET status=? WHERE slot=? AND status=?',('isolated',slot_id,'active'))
    return record(db,'heartbeat',slot_id,'healthy' if healthy else 'isolated',{})

def repay(db,jid,slot_id,at):
    lease=row(db,'SELECT * FROM leases WHERE job=?',(jid,))
    slot=row(db,'SELECT * FROM slots WHERE id=?',(slot_id,))
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if not lease or lease['status']!='preempted' or not lease['debt']:
        return record(db,'repay',jid,'no_debt',{})
    if not slot or not slot['healthy'] or slot['cpu']<job['cpu'] or at+lease['debt']>job['deadline']:
        return record(db,'repay',jid,'unrepayable',{'debt':lease['debt']})
    occupied=row(db,'SELECT job FROM leases WHERE slot=? AND status=?',(slot_id,'active'))
    if occupied:return record(db,'repay',jid,'occupied',{'by':occupied['job']})
    generation=lease['generation']+1
    db.execute('UPDATE leases SET slot=?,generation=?,expires=?,status=?,debt=0 WHERE job=?',(slot_id,generation,at+lease['debt'],'active',jid))
    return record(db,'repay',jid,'repaid',{'slot':slot_id,'generation':generation})
