"""Whole-timeline feasibility checks.

A lease is a *fixed reservation*: its cpu/memory vector is physically charged
to the node for every reserved minute, no matter which slot id or tenant owns
it. Everything else (unleased work and preemption debt) is *flexible work*
that still has to be packable before its deadline. :func:`feasible` replays
the timeline minute by minute so a preemption is only accepted when the whole
remaining plan still honors real node capacity, parent delivery and every
deadline -- never by looking at the current minute alone.
"""
from .store import row, rows

# Only a live active lease holds physical minutes. An isolated lease lives on
# a node that can no longer be reached: its remaining work is owed elsewhere
# and enters the flexible pool rather than occupying a node it cannot use.
FIXED_STATES = ('active',)


def _node_capacity(db, slots, nodes):
    caps = {n['node']: {'cpu': n['cpu'], 'memory': n['memory']} for n in nodes}
    # Nodes only known through slot registration keep the physical size that
    # was declared when the first slot appeared.
    for slot in slots:
        caps.setdefault(slot['node'], {'cpu': slot['cpu'], 'memory': slot['memory']})
    return caps


def _completed_minutes(db, jid, now=None):
    total = 0
    for attempt in rows(db, 'SELECT started,ended,status FROM attempts WHERE job=?', (jid,)):
        if attempt['ended'] is not None:
            total += max(0, attempt['ended'] - attempt['started'])
        elif attempt['status'] == 'running' and now is not None:
            total += max(0, now - attempt['started'])
    return total


def remaining_work(db, job, lease, now):
    """Work still owed for a job, in minutes. Preemption debt is authoritative."""
    if job['status'] in ('delivered', 'cancelled', 'blocked_parent', 'breached'):
        return 0
    if lease and lease['status'] == 'preempted' and lease['debt']:
        return lease['debt']
    return max(0, job['duration'] - _completed_minutes(db, job['id'], now))


def _load_state(db, lease_overrides, job_overrides):
    jobs = {j['id']: dict(j) for j in rows(db, 'SELECT * FROM jobs')}
    leases = {l['job']: dict(l) for l in rows(db, 'SELECT * FROM leases')}
    slots = [dict(s) for s in rows(db, 'SELECT * FROM slots')]
    nodes = [dict(n) for n in rows(db, 'SELECT * FROM nodes')]
    parents = {}
    for dep in rows(db, 'SELECT job,parent FROM dependencies'):
        parents.setdefault(dep['job'], []).append(dep['parent'])
    delivered = {a['job'] for a in rows(db, "SELECT job FROM artifacts WHERE status='delivered'")}
    for jid, patch in (job_overrides or {}).items():
        if patch is None:
            jobs.pop(jid, None)
            leases.pop(jid, None)
        elif jid in jobs:
            jobs[jid].update(patch)
    for jid, patch in (lease_overrides or {}).items():
        if patch is None:
            leases.pop(jid, None)
        else:
            leases[jid] = dict(patch)
    return jobs, leases, slots, nodes, parents, delivered


def feasible(db, now, lease_overrides=None, job_overrides=None):
    """Return ``(ok, reason, evidence)`` for the projected world state."""
    jobs, leases, slots, nodes, parents, delivered = _load_state(db, lease_overrides, job_overrides)
    slot_by_id = {s['id']: s for s in slots}
    caps = _node_capacity(db, slots, nodes)

    fixed = [l for l in leases.values() if l['status'] in FIXED_STATES and l['job'] in jobs]
    flex = {}
    horizon = now
    for job in jobs.values():
        lease = leases.get(job['id'])
        rem = remaining_work(db, job, lease, now)
        if rem <= 0:
            continue
        horizon = max(horizon, job['deadline'])
        if not lease or lease['status'] not in FIXED_STATES:
            flex[job['id']] = rem

    # 1. Fixed reservations are a hard per-node, per-minute capacity ceiling.
    # Two slots on the same physical node cannot sell the same core twice.
    usage = {}
    for lease in fixed:
        slot = slot_by_id.get(lease['slot'])
        if not slot:
            return False, 'node_overcommit', {'lease': lease['job'], 'reason': 'slot_missing'}
        job = jobs[lease['job']]
        cap = caps[slot['node']]
        if job['cpu'] > cap['cpu'] or job['memory'] > cap['memory']:
            return False, 'node_overcommit', {'node': slot['node'], 'job': lease['job'],
                                              'requested': {'cpu': job['cpu'], 'memory': job['memory']},
                                              'capacity': cap}
        for minute in range(max(now, lease.get('granted') or now), lease['expires']):
            cell = usage.setdefault((slot['node'], minute), {'cpu': 0, 'memory': 0, 'jobs': []})
            cell['cpu'] += job['cpu']
            cell['memory'] += job['memory']
            cell['jobs'].append(lease['job'])
            if cell['cpu'] > cap['cpu'] or cell['memory'] > cap['memory']:
                return False, 'node_overcommit', {'node': slot['node'], 'minute': minute,
                                                  'used': {'cpu': cell['cpu'], 'memory': cell['memory']},
                                                  'capacity': cap, 'jobs': list(cell['jobs'])}

    remaining = {jid: rem for jid, rem in flex.items()}
    fixed_by_job = {l['job']: l for l in fixed}
    for jid, lease in fixed_by_job.items():
        remaining[jid] = remaining_work(db, jobs[jid], lease, now)

    # Deliveries produced inside the replay must also unblock children; the
    # real delivered set seeds it for work already on the record.
    simulated_delivered = set(delivered)

    def is_ready(jid):
        return all(p in simulated_delivered for p in parents.get(jid, ()))

    def healthy_slot(slot):
        # An explicit isolation mark fences the slot immediately, regardless
        # of how fresh the last heartbeat was.
        return bool(slot['healthy']) and now <= slot['heartbeat'] + 60

    # 2. Replay flexible work with EDF against the capacity left on each node
    # after fixed reservations. Fixed work spills into the flexible pool once
    # its reservation window ends, so an unpayable preemption debt surfaces
    # here instead of at breach time. Every minute, the slots already held by
    # fixed (or previously placed flexible) jobs are removed before EDF
    # placement: free node capacity alone must never sell the same slot twice.
    for minute in range(now, max(horizon, now + 1) + 1):
        for jid, lease in list(fixed_by_job.items()):
            # The reservation ends at its expiry whether or not the work
            # finished inside the window; holding a finished job would keep
            # its slot fenced off forever.
            if minute >= lease['expires']:
                fixed_by_job.pop(jid)
        # Fixed reservations execute inside their own windows: advance their
        # work minute by minute on the reserving slot, which stays consumed.
        for jid, lease in list(fixed_by_job.items()):
            if not is_ready(jid):
                continue
            slot = slot_by_id.get(lease['slot'])
            if slot and healthy_slot(slot) and remaining.get(jid, 0) > 0 and minute < lease['expires']:
                remaining[jid] -= 1
                if remaining[jid] == 0:
                    simulated_delivered.add(jid)
        held_slots = {lease['slot'] for lease in fixed_by_job.values()}
        # Build the per-node free capacity and free-slot pool once for the
        # minute, then place flexible jobs in one global EDF order so an
        # urgent deadline is never starved by a lazier job on another node.
        node_state = {}
        for node, cap in caps.items():
            cell = usage.get((node, minute), {'cpu': 0, 'memory': 0})
            node_state[node] = {
                'free_cpu': cap['cpu'] - cell['cpu'],
                'free_mem': cap['memory'] - cell['memory'],
                'slots': [s for s in slots
                          if s['node'] == node and healthy_slot(s) and s['id'] not in held_slots],
            }
        candidates = sorted(
            (jid for jid, rem in remaining.items()
             if rem > 0 and jid not in fixed_by_job and is_ready(jid)
             and jobs[jid]['deadline'] > minute),
            key=lambda x: (jobs[x]['deadline'], x))
        for jid in candidates:
            job = jobs[jid]
            home = None
            home_node = None
            # Prefer the node with the least fitting free capacity to leave
            # room for larger vectors elsewhere (best-fit).
            options = []
            for node, state in node_state.items():
                slot = next((s for s in state['slots']
                             if s['cpu'] >= job['cpu'] and s['memory'] >= job['memory']
                             and job['cpu'] <= state['free_cpu']
                             and job['memory'] <= state['free_mem']), None)
                if slot:
                    options.append((state['free_cpu'], state['free_mem'], node, slot))
            if options:
                options.sort()
                _, _, home_node, home = options[0]
            if not home:
                continue
            state = node_state[home_node]
            state['slots'].remove(home)
            held_slots.add(home['id'])
            state['free_cpu'] -= job['cpu']
            state['free_mem'] -= job['memory']
            remaining[jid] -= 1
            if remaining[jid] == 0:
                simulated_delivered.add(jid)
        # A fixed job that finishes inside its own window also unblocks heirs.
        for jid, rem in list(remaining.items()):
            if rem == 0:
                simulated_delivered.add(jid)
        overdue = [jid for jid, rem in remaining.items()
                   if rem > 0 and jobs[jid]['deadline'] <= minute]
        if overdue:
            return False, 'timeline_infeasible', {'minute': minute, 'jobs': sorted(overdue)}

    unpaid = sorted(jid for jid, rem in remaining.items() if rem > 0)
    if unpaid:
        return False, 'timeline_infeasible', {'jobs': unpaid}
    return True, 'feasible', {}
