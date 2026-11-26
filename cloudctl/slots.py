from .store import row, rows, record
from .planner import feasible

# Reservations that still own the slot from an operator's point of view,
# including leases fenced off by an isolation event.
FIXED_STATES = ('active', 'isolated')


def _done_minutes(db, jid, now=None):
    """Work already performed, including the still-running attempt."""
    total = 0
    for attempt in rows(db, 'SELECT started,ended,status FROM attempts WHERE job=?', (jid,)):
        if attempt['ended'] is not None:
            total += max(0, attempt['ended'] - attempt['started'])
        elif attempt['status'] == 'running' and now is not None:
            total += max(0, now - attempt['started'])
    return total


def _ensure_node(db, request, cpu, memory):
    node = row(db, 'SELECT * FROM nodes WHERE node=?', (request['node'],))
    if node is None:
        db.execute('INSERT INTO nodes VALUES(?,?,?,?,?)',
                   (request['node'], cpu, memory, request['at'], 1))
        return None
    if cpu > node['cpu'] or memory > node['memory']:
        return record(db, 'node', request['node'], 'node_overcommit',
                      {'declared': {'cpu': cpu, 'memory': memory},
                       'physical': {'cpu': node['cpu'], 'memory': node['memory']}})
    return None


def register_node(db, request):
    cpu, memory = request['cpu'], request.get('memory', 4)
    if min(cpu, memory) <= 0:
        return record(db, 'node', request['node'], 'invalid_resource', {})
    old = row(db, 'SELECT * FROM nodes WHERE node=?', (request['node'],))
    now = request['at']
    # A derating is accepted only when every fixed reservation still fits the
    # smaller physical envelope from now on; delivered artifacts and history
    # are never rewritten.
    if old and (cpu < old['cpu'] or memory < old['memory']):
        leases = rows(db, "SELECT l.*,j.cpu AS jcpu,j.memory AS jmem,s.node AS node FROM leases l "
                          "JOIN jobs j ON j.id=l.job JOIN slots s ON s.id=l.slot "
                          "WHERE s.node=? AND l.status IN ('active','isolated')", (request['node'],))
        usage = {}
        for lease in leases:
            for minute in range(max(now, lease.get('granted') or now), lease['expires']):
                cell = usage.setdefault(minute, {'cpu': 0, 'memory': 0, 'jobs': []})
                cell['cpu'] += lease['jcpu']
                cell['memory'] += lease['jmem']
                cell['jobs'].append(lease['job'])
                if cell['cpu'] > cpu or cell['memory'] > memory:
                    return record(db, 'node', request['node'], 'node_overcommit',
                                  {'minute': minute, 'used': {'cpu': cell['cpu'], 'memory': cell['memory']},
                                   'capacity': {'cpu': cpu, 'memory': memory}, 'jobs': list(cell['jobs'])})
    db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)',
               (request['node'], cpu, memory, now, 1 if old is None else old['healthy']))
    return record(db, 'node', request['node'], 'ready',
                  {'cpu': cpu, 'memory': memory, 'derated': bool(old and (cpu < old['cpu'] or memory < old['memory']))})


def register(db, request):
    cpu, memory = request['cpu'], request.get('memory', 4)
    if min(cpu, memory) <= 0:
        return record(db, 'slot', request['id'], 'invalid_resource', {})
    old = row(db, 'SELECT * FROM slots WHERE id=?', (request['id'],))
    if old and (old['cpu'] != cpu or old['memory'] != memory):
        return record(db, 'slot', request['id'], 'conflict', {})
    rejected = _ensure_node(db, request, cpu, memory)
    if rejected:
        return rejected
    db.execute('INSERT OR REPLACE INTO slots VALUES(?,?,?,?,?,?)',
               (request['id'], cpu, memory, request['node'], 1, request['at']))
    return record(db, 'slot', request['id'], 'ready', {'cpu': cpu, 'memory': memory, 'node': request['node']})


def _lease_row(lease, **patch):
    projected = dict(lease)
    projected.update(patch)
    return projected


def lease(db, jid, slot_id, now, expires, preempt=False):
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (slot_id,))
    if not job or job['status'] != 'accepted':
        return record(db, 'lease', jid, 'job_unavailable', {})
    if expires <= now:
        return record(db, 'lease', jid, 'invalid_window', {})
    if not slot or not slot['healthy'] or now - slot['heartbeat'] > 60:
        return record(db, 'lease', jid, 'slot_unavailable', {})
    node = row(db, 'SELECT * FROM nodes WHERE node=?', (slot['node'],))
    node_cpu = node['cpu'] if node else slot['cpu']
    node_mem = node['memory'] if node else slot['memory']
    if job['cpu'] > slot['cpu'] or job['memory'] > slot['memory'] or expires > job['deadline']:
        return record(db, 'lease', jid, 'capacity_or_deadline',
                      {'requested': {'cpu': job['cpu'], 'memory': job['memory']},
                       'available': {'cpu': slot['cpu'], 'memory': slot['memory']}})
    if job['cpu'] > node_cpu or job['memory'] > node_mem:
        return record(db, 'lease', jid, 'node_overcommit',
                      {'node': slot['node'], 'requested': {'cpu': job['cpu'], 'memory': job['memory']},
                       'physical': {'cpu': node_cpu, 'memory': node_mem}})

    active = rows(db, 'SELECT * FROM leases WHERE slot=? AND status IN (?,?)',
                  (slot_id, 'active', 'isolated'))
    if active and not preempt:
        return record(db, 'lease', jid, 'occupied', {'by': [a['job'] for a in active]})

    overrides = {}
    preempted = []
    for victim_lease in active:
        if row(db, 'SELECT status FROM artifacts WHERE job=?', (victim_lease['job'],)):
            return record(db, 'lease', jid, 'delivered_cannot_preempt', {})
        victim_job = row(db, 'SELECT * FROM jobs WHERE id=?', (victim_lease['job'],))
        debt = max(0, victim_job['duration'] - _done_minutes(db, victim_lease['job'], now))
        preempted.append({'job': victim_lease['job'], 'debt': debt})
        overrides[victim_lease['job']] = _lease_row(victim_lease, status='preempted', debt=debt)

    current = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    generation = (current or {}).get('generation', 0) + 1
    projected = {'job': jid, 'slot': slot_id, 'generation': generation,
                 'expires': expires, 'status': 'active', 'debt': 0, 'granted': now}
    overrides[jid] = projected

    ok, reason, evidence = feasible(db, now, lease_overrides=overrides)
    if not ok:
        # The whole remaining timeline must stay deliverable before any lease
        # is touched; a preemption that strands debt is refused up front.
        return record(db, 'lease', jid, reason, evidence)

    for item in preempted:
        db.execute('UPDATE leases SET status=?,debt=? WHERE job=?',
                   ('preempted', item['debt'], item['job']))
        # Work already executed is history, never rewritten as "never ran";
        # the running attempt is closed so an old-generation settle fails.
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND status=?',
                   ('preempted', now, item['job'], 'running'))
    db.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?,?,?)',
               (jid, slot_id, generation, expires, 'active', 0, now))
    return record(db, 'lease', jid, 'leased',
                  {'slot': slot_id, 'generation': generation,
                   'preempted': [item['job'] for item in preempted]})


def heartbeat(db, slot_id, at, healthy):
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (slot_id,))
    if not slot:
        return record(db, 'heartbeat', slot_id, 'missing', {})
    if at < slot['heartbeat']:
        return record(db, 'heartbeat', slot_id, 'stale', {})
    db.execute('UPDATE slots SET heartbeat=?,healthy=? WHERE id=?', (at, int(healthy), slot_id))
    if not healthy:
        db.execute('UPDATE leases SET status=? WHERE slot=? AND status=?', ('isolated', slot_id, 'active'))
        return record(db, 'heartbeat', slot_id, 'isolated', {})
    # A recovered node cannot resurrect leases it was fenced off from: late
    # successes from the old generation must never take delivery back.
    fenced = rows(db, 'SELECT job FROM leases WHERE slot=? AND status=?', (slot_id, 'isolated'))
    for item in fenced:
        db.execute('UPDATE leases SET status=? WHERE job=?', ('fenced', item['job']))
    return record(db, 'heartbeat', slot_id, 'healthy',
                  {'fenced': [item['job'] for item in fenced]})


def repay(db, jid, slot_id, at):
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    slot = row(db, 'SELECT * FROM slots WHERE id=?', (slot_id,))
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not lease or lease['status'] != 'preempted' or not lease['debt']:
        return record(db, 'repay', jid, 'no_debt', {})
    if not slot or not slot['healthy'] or slot['cpu'] < job['cpu'] or slot['memory'] < job['memory'] \
            or at + lease['debt'] > job['deadline']:
        return record(db, 'repay', jid, 'unrepayable', {'debt': lease['debt']})
    occupied = row(db, "SELECT job FROM leases WHERE slot=? AND status IN ('active','isolated')", (slot_id,))
    if occupied:
        return record(db, 'repay', jid, 'occupied', {'by': occupied['job']})
    generation = lease['generation'] + 1
    projected = {'job': jid, 'slot': slot_id, 'generation': generation,
                 'expires': at + lease['debt'], 'status': 'active', 'debt': 0, 'granted': at}
    ok, reason, evidence = feasible(db, at, lease_overrides={jid: projected})
    if not ok:
        return record(db, 'repay', jid, reason, evidence)
    db.execute('UPDATE leases SET slot=?,generation=?,expires=?,status=?,debt=0,granted=? WHERE job=?',
               (slot_id, generation, at + lease['debt'], 'active', at, jid))
    return record(db, 'repay', jid, 'repaid', {'slot': slot_id, 'generation': generation})


def release(db, jid, at, reason):
    """Operator-side surrender of a reservation.

    Started work is preserved as preemption debt; a reservation that never ran
    is simply released so its physical minutes can be re-sold. No attempt or
    artifact row is ever deleted.
    """
    lease = row(db, 'SELECT * FROM leases WHERE job=?', (jid,))
    job = row(db, 'SELECT * FROM jobs WHERE id=?', (jid,))
    if not lease or lease['status'] not in FIXED_STATES:
        return record(db, 'release', jid, 'not_leased', {})
    if not job or job['status'] != 'accepted':
        return record(db, 'release', jid, 'job_unavailable', {'status': job['status'] if job else None})
    running = bool(row(db, 'SELECT 1 FROM attempts WHERE job=? AND status=?', (jid, 'running')))
    if running:
        done = _done_minutes(db, jid, at)
        debt = max(0, job['duration'] - done)
        db.execute('UPDATE leases SET status=?,debt=? WHERE job=?', ('preempted', debt, jid))
        # The running attempt can no longer settle against the fenced lease.
        db.execute('UPDATE attempts SET status=?,ended=? WHERE job=? AND status=?',
                   ('preempted', at, jid, 'running'))
        return record(db, 'release', jid, 'preempted', {'debt': debt, 'reason': reason})
    db.execute('UPDATE leases SET status=? WHERE job=?', ('released', jid))
    return record(db, 'release', jid, 'released', {'reason': reason})
