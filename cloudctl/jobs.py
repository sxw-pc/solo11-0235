from .store import row,rows,record

def submit(db,request):
    jid=request['id']; parents=sorted(set(request.get('parents',[])))
    old=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if old:
        return record(db,'submit',jid,'idempotent' if old['deadline']==request['deadline'] else 'conflict',{'old_version':old['version']})
    if jid in parents:return record(db,'submit',jid,'rejected_cycle',{'parents':parents})
    missing=[p for p in parents if not row(db,'SELECT id FROM jobs WHERE id=? AND status=?',(p,'accepted'))]
    if missing:return record(db,'submit',jid,'rejected_dependency',{'missing':missing})
    if request['deadline']<=request['created']+request['duration']:
        return record(db,'submit',jid,'rejected_deadline',{'deadline':request['deadline']})
    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',(jid,request['tenant'],request['deadline'],request['duration'],request['cpu'],'accepted',1,request['created']))
    db.executemany('INSERT INTO dependencies VALUES(?,?)',[(jid,p) for p in parents])
    return record(db,'submit',jid,'accepted',{'parents':parents,'version':1})

def revise(db,jid,deadline,expected):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if not job:return record(db,'revise',jid,'missing',{})
    if job['version']!=expected:return record(db,'revise',jid,'stale_version',{'current':job['version']})
    if row(db,'SELECT job FROM attempts WHERE job=?',(jid,)):
        return record(db,'revise',jid,'already_started',{})
    if deadline<=job['created']+job['duration']:
        return record(db,'revise',jid,'rejected_deadline',{})
    children=rows(db,'SELECT job FROM dependencies WHERE parent=?',(jid,))
    blocked=[c['job'] for c in children if row(db,'SELECT deadline FROM jobs WHERE id=?',(c['job'],))['deadline']<deadline]
    if blocked:return record(db,'revise',jid,'blocked_successors',{'jobs':blocked})
    db.execute('UPDATE jobs SET deadline=?,version=version+1 WHERE id=?',(deadline,jid))
    return record(db,'revise',jid,'revised',{'version':expected+1,'successors':children})

def cancel(db,jid,at,reason):
    job=row(db,'SELECT * FROM jobs WHERE id=?',(jid,))
    if not job:return record(db,'cancel',jid,'missing',{})
    if job['status']=='cancelled':return record(db,'cancel',jid,'idempotent',{})
    if job['status']=='delivered':return record(db,'cancel',jid,'already_delivered',{})
    generation=(row(db,'SELECT generation FROM cancellations WHERE job=?',(jid,)) or {}).get('generation',0)+1
    db.execute('INSERT OR REPLACE INTO cancellations VALUES(?,?,?,?)',(jid,generation,reason,at))
    db.execute('UPDATE jobs SET status=?,version=version+1 WHERE id=?',('cancelled',jid))
    db.execute('UPDATE leases SET status=? WHERE job=? AND status=?',('released',jid,'active'))
    dependents=rows(db,'SELECT job FROM dependencies WHERE parent=?',(jid,))
    for child in dependents:db.execute('UPDATE jobs SET status=? WHERE id=? AND status=?',('blocked_parent',child['job'],'accepted'))
    return record(db,'cancel',jid,'cancelled',{'generation':generation,'blocked':[x['job'] for x in dependents]})
