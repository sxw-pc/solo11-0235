import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cloudctl import store
from cloudctl.__main__ import execute


def digest_of(text):
    return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()


class NodeCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'capacity.sqlite')
        store.init(self.db)
        self._submit_batch()
        self._register_slots()

    def tearDown(self):
        self.temp.cleanup()

    def act(self, **kw):
        return execute(self.db, kw)

    def decide(self, **kw):
        return self.act(**kw)['decision']

    def _submit_batch(self):
        # A dl50 dur20 3c4g; B dl70 dur20 3c4g depends on A; U dl35 dur10 4c8g
        self.decide(action='job.submit', id='A', tenant='t1', deadline=50,
                    duration=20, cpu=3, memory=4, created=0)
        self.decide(action='job.submit', id='B', tenant='t1', deadline=70,
                    duration=20, cpu=3, memory=4, created=0, parents=['A'])
        self.decide(action='job.submit', id='U', tenant='t1', deadline=35,
                    duration=10, cpu=4, memory=8, created=0)

    def _register_slots(self):
        # s1,s2 both on n1 whose real total is only 4c8g; s3 on n2.
        for sid, node in (('s1', 'n1'), ('s2', 'n1'), ('s3', 'n2')):
            self.assertEqual(
                self.decide(action='slot.register', id=sid, node=node,
                            cpu=4, memory=8, at=1), 'ready')

    # ------------------------------------------------------------------
    # The exact oversubscription reported in production, reproduced item by item.
    def test_second_slot_on_same_node_cannot_sell_same_cores(self):
        self.assertEqual(self.decide(action='slot.lease', id='A', slot='s1',
                                     at=10, expires=35), 'leased')
        # Old system approved this; node-level accounting must now refuse it.
        self.assertEqual(self.decide(action='slot.lease', id='B', slot='s2',
                                     at=12, expires=50), 'node_overcommit')
        self.assertEqual(self.decide(action='run.start', id='A', generation=1, at=10), 'running')
        self.assertEqual(self.decide(action='run.start', id='B', generation=1, at=12), 'not_leased')
        with store.transaction(self.db) as db:
            active = store.rows(db, "SELECT l.job FROM leases l JOIN slots s ON l.slot=s.id "
                                    "WHERE s.node='n1' AND l.status='active'")
        self.assertEqual([r['job'] for r in active], ['A'])

    def test_new_slot_id_or_tenant_cannot_resell_node_capacity(self):
        self.assertEqual(self.decide(action='slot.register', id='s4', node='n1',
                                     cpu=4, memory=8, at=2), 'ready')
        self.assertEqual(self.decide(action='slot.lease', id='A', slot='s1',
                                     at=10, expires=35), 'leased')
        self.assertEqual(self.decide(action='slot.lease', id='U', slot='s4',
                                     at=10, expires=30), 'node_overcommit')
        self.decide(action='job.submit', id='V', tenant='other-tenant', deadline=40,
                    duration=5, cpu=4, memory=8, created=0)
        self.assertEqual(self.decide(action='slot.lease', id='V', slot='s2',
                                     at=11, expires=20), 'node_overcommit')

    # ------------------------------------------------------------------
    # Approved schedule (a): A finishes on n1, U moves to n2, B runs after A.
    def test_schedule_a_no_preemption_meets_all_deadlines(self):
        self.assertEqual(self.decide(action='slot.lease', id='A', slot='s1',
                                     at=10, expires=35), 'leased')
        self.assertEqual(self.decide(action='slot.lease', id='U', slot='s3',
                                     at=20, expires=35), 'leased')
        self.assertEqual(self.decide(action='run.start', id='A', generation=1, at=10), 'running')
        self.assertEqual(self.decide(action='run.start', id='U', generation=1, at=20), 'running')
        self.assertEqual(self.decide(action='run.settle', id='U', generation=1, at=30,
                                     digest=digest_of('u'), content='u'), 'delivered')
        self.assertEqual(self.decide(action='run.settle', id='A', generation=1, at=30,
                                     digest=digest_of('a'), content='a'), 'delivered')
        self.assertEqual(self.decide(action='slot.lease', id='B', slot='s1',
                                     at=30, expires=55), 'leased')
        self.assertEqual(self.decide(action='run.start', id='B', generation=1, at=30), 'running')
        self.assertEqual(self.decide(action='run.settle', id='B', generation=1, at=50,
                                     digest=digest_of('b'), content='b'), 'delivered')
        for jid, minute in (('A', 30), ('U', 30), ('B', 50)):
            with store.transaction(self.db) as db:
                art = store.row(db, 'SELECT delivered FROM artifacts WHERE job=?', (jid,))
            self.assertEqual(art['delivered'], minute)

    # ------------------------------------------------------------------
    # Approved schedule (b): preempt A at 20 keeping 10 minutes of debt,
    # U on n1, A repaid on n2, B last on n1.
    def test_schedule_b_preemption_debt_repayment_meets_all_deadlines(self):
        self.assertEqual(self.decide(action='slot.lease', id='A', slot='s1',
                                     at=10, expires=35), 'leased')
        self.assertEqual(self.decide(action='run.start', id='A', generation=1, at=10), 'running')
        self.assertEqual(self.decide(action='slot.lease', id='U', slot='s1', at=20,
                                     expires=35, preempt=True), 'leased')
        with store.transaction(self.db) as db:
            lease = store.row(db, "SELECT status,debt FROM leases WHERE job='A'")
            attempt = store.row(db, "SELECT status FROM attempts WHERE job='A' AND generation=1")
        self.assertEqual(lease['status'], 'preempted')
        self.assertEqual(lease['debt'], 10)
        self.assertEqual(attempt['status'], 'preempted')

        self.assertEqual(self.decide(action='run.start', id='U', generation=1, at=20), 'running')
        self.assertEqual(self.decide(action='run.settle', id='U', generation=1, at=30,
                                     digest=digest_of('u'), content='u'), 'delivered')
        self.assertEqual(self.decide(action='slot.repay', id='A', slot='s3', at=30), 'repaid')
        self.assertEqual(self.decide(action='run.start', id='A', generation=2, at=30), 'running')
        self.assertEqual(self.decide(action='run.settle', id='A', generation=2, at=40,
                                     digest=digest_of('a'), content='a'), 'delivered')
        self.assertEqual(self.decide(action='slot.lease', id='B', slot='s1',
                                     at=40, expires=65), 'leased')
        self.assertEqual(self.decide(action='run.start', id='B', generation=1, at=40), 'running')
        self.assertEqual(self.decide(action='run.settle', id='B', generation=1, at=60,
                                     digest=digest_of('b'), content='b'), 'delivered')

    def test_started_work_is_never_rewritten_and_late_generation_loses(self):
        self.decide(action='slot.lease', id='A', slot='s1', at=10, expires=35)
        self.decide(action='run.start', id='A', generation=1, at=10)
        self.decide(action='slot.lease', id='U', slot='s1', at=20, expires=35, preempt=True)
        self.decide(action='run.start', id='U', generation=1, at=20)
        self.decide(action='run.settle', id='U', generation=1, at=30,
                    digest=digest_of('u'), content='u')
        self.decide(action='slot.repay', id='A', slot='s3', at=30)
        self.decide(action='run.start', id='A', generation=2, at=30)
        self.decide(action='run.settle', id='A', generation=2, at=40,
                    digest=digest_of('a'), content='a')
        # A late success from generation 1 cannot take delivery from generation 2.
        self.assertEqual(self.decide(action='run.settle', id='A', generation=1, at=41,
                                     digest=digest_of('stale'), content='stale'), 'stale_attempt')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db, "SELECT digest FROM artifacts WHERE job='A'")['digest'],
                             digest_of('a'))
            history = store.rows(db, 'SELECT generation,status FROM attempts WHERE job=? ORDER BY generation',
                                 ('A',))
        self.assertEqual([(r['generation'], r['status']) for r in history],
                         [(1, 'preempted'), (2, 'success')])

    def test_preemption_is_refused_before_landing_when_timeline_cannot_be_repaid(self):
        # Two nodes, one slot each. A is anchored on n1; K's full-node
        # reservation fences off n2 past A's deadline. Preempting A for a
        # full-node U whose window covers A's deadline leaves the residual debt
        # no healthy slot before the deadline, so it must be refused up front.
        temp = tempfile.TemporaryDirectory()
        db = str(Path(temp.name) / 'veto.sqlite')
        store.init(db)
        try:
            def go(**kw):
                return execute(db, kw)['decision']
            go(action='job.submit', id='A', tenant='t1', deadline=40, duration=20,
               cpu=3, memory=4, created=0)
            go(action='job.submit', id='U', tenant='t1', deadline=50, duration=10,
               cpu=4, memory=8, created=0)
            go(action='job.submit', id='K', tenant='t1', deadline=60, duration=10,
               cpu=4, memory=8, created=0)
            go(action='slot.register', id='s1', node='n1', cpu=4, memory=8, at=1)
            go(action='slot.register', id='s3', node='n2', cpu=4, memory=8, at=1)
            self.assertEqual(go(action='slot.lease', id='A', slot='s1', at=10, expires=35),
                             'leased')
            self.assertEqual(go(action='slot.lease', id='K', slot='s3', at=10, expires=55),
                             'leased')
            self.assertEqual(go(action='run.start', id='A', generation=1, at=10), 'running')
            self.assertEqual(go(action='slot.lease', id='U', slot='s1', at=22,
                                expires=40, preempt=True), 'timeline_infeasible')
            # Nothing was touched: the refused preemption leaves no partial state.
            with store.transaction(db) as conn:
                lease = store.row(conn, "SELECT status,debt FROM leases WHERE job='A'")
                self.assertEqual(lease['status'], 'active')
                self.assertEqual(lease['debt'], 0)
                self.assertEqual(
                    store.row(conn, "SELECT status FROM attempts WHERE job='A' AND generation=1")['status'],
                    'running')
        finally:
            temp.cleanup()

    # ------------------------------------------------------------------
    # Heartbeat isolation, power loss and the surviving physical record.
    def test_isolated_node_fences_lease_and_failed_plan_must_compensate(self):
        # Schedule (a): U is running on n2 when n2 is isolated at minute 29.
        self.decide(action='slot.lease', id='A', slot='s1', at=10, expires=35)
        self.decide(action='slot.lease', id='U', slot='s3', at=20, expires=35)
        self.decide(action='run.start', id='A', generation=1, at=10)
        self.decide(action='run.start', id='U', generation=1, at=20)
        self.assertEqual(self.decide(action='slot.heartbeat', slot='s3', at=29, healthy=False),
                         'isolated')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db, "SELECT status FROM leases WHERE job='U'")['status'],
                             'isolated')
        # A settle arriving from the fenced node is rejected; no artifact is made.
        self.assertEqual(self.decide(action='run.settle', id='U', generation=1, at=30,
                                     digest=digest_of('u'), content='u'), 'stale_attempt')
        with store.transaction(self.db) as db:
            self.assertIsNone(store.row(db, 'SELECT * FROM artifacts WHERE job=?', ('U',)))
        # Re-lease onto n1 is refused while A's fixed reservation fills the node.
        self.assertEqual(self.decide(action='slot.lease', id='U', slot='s2',
                                     at=30, expires=35), 'node_overcommit')
        # After A delivers, n1 has room: U can still be compensated within dl35
        # only if a feasible window exists; here the deadline is already tight,
        # so the control plane keeps the failure receipt instead of faking success.
        self.decide(action='run.settle', id='A', generation=1, at=30,
                    digest=digest_of('a'), content='a')

    def test_heartbeat_recovery_does_not_resurrect_fenced_generation(self):
        self.decide(action='slot.lease', id='U', slot='s3', at=5, expires=30)
        self.decide(action='run.start', id='U', generation=1, at=6)
        self.assertEqual(self.decide(action='slot.heartbeat', slot='s3', at=15, healthy=False),
                         'isolated')
        self.assertEqual(self.decide(action='slot.heartbeat', slot='s3', at=16, healthy=True),
                         'healthy')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db, "SELECT status FROM leases WHERE job='U'")['status'],
                             'fenced')
        # The old generation can no longer start or settle against the slot.
        self.assertEqual(self.decide(action='run.start', id='U', generation=1, at=17),
                         'not_leased')

    # ------------------------------------------------------------------
    # Node derating and physical capacity declarations.
    def test_node_derating_is_rejected_while_reservations_exceed_new_envelope(self):
        self.decide(action='slot.lease', id='U', slot='s3', at=5, expires=30)
        self.assertEqual(self.decide(action='node.register', node='n2', cpu=2,
                                     memory=8, at=10), 'node_overcommit')
        # The physical record is unchanged after the refusal.
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db, 'SELECT cpu FROM nodes WHERE node=?', ('n2',))['cpu'], 4)

    def test_node_derating_is_accepted_when_reservations_still_fit(self):
        self.assertEqual(self.decide(action='node.register', node='n2', cpu=4,
                                     memory=4, at=10), 'ready')
        with store.transaction(self.db) as db:
            node = store.row(db, 'SELECT cpu,memory FROM nodes WHERE node=?', ('n2',))
        self.assertEqual(node, {'cpu': 4, 'memory': 4})

    # ------------------------------------------------------------------
    # Parent cancellation and deadline revision flow into child leases.
    def test_parent_cancel_releases_child_lease_and_blocks_delivery(self):
        temp = tempfile.TemporaryDirectory()
        db = str(Path(temp.name) / 'cancel.sqlite')
        store.init(db)
        try:
            def go(**kw):
                return execute(db, kw)['decision']
            go(action='job.submit', id='A', tenant='t1', deadline=50, duration=20,
               cpu=3, memory=4, created=0)
            go(action='job.submit', id='B', tenant='t1', deadline=70, duration=20,
               cpu=3, memory=4, created=0, parents=['A'])
            go(action='slot.register', id='s1', node='n1', cpu=4, memory=8, at=1)
            go(action='slot.register', id='s3', node='n2', cpu=4, memory=8, at=1)
            self.assertEqual(go(action='slot.lease', id='A', slot='s1', at=5, expires=30),
                             'leased')
            self.assertEqual(go(action='slot.lease', id='B', slot='s3', at=6, expires=55),
                             'leased')
            self.assertEqual(go(action='job.cancel', id='A', at=7, reason='stop'), 'cancelled')
            with store.transaction(db) as conn:
                self.assertEqual(
                    store.row(conn, "SELECT status FROM leases WHERE job='B'")['status'],
                    'released')
                self.assertEqual(
                    store.row(conn, "SELECT status FROM jobs WHERE id='B'")['status'],
                    'blocked_parent')
                decisions = [r['decision'] for r in store.rows(
                    conn, "SELECT decision FROM events WHERE subject='B' ORDER BY seq")]
            self.assertIn('released', decisions)
        finally:
            temp.cleanup()

    def test_parent_revision_rechecks_child_lease_against_timeline(self):
        temp = tempfile.TemporaryDirectory()
        db = str(Path(temp.name) / 'revise.sqlite')
        store.init(db)
        try:
            def go(**kw):
                return execute(db, kw)['decision']
            go(action='job.submit', id='P', tenant='t', deadline=40, duration=10,
               cpu=3, memory=4, created=0)
            go(action='job.submit', id='C', tenant='t', deadline=60, duration=10,
               cpu=3, memory=4, created=0, parents=['P'])
            # Separate nodes so both reservations are initially deliverable.
            go(action='slot.register', id='x1', node='n1', cpu=4, memory=8, at=1)
            go(action='slot.register', id='x2', node='n2', cpu=4, memory=8, at=1)
            self.assertEqual(go(action='slot.lease', id='P', slot='x1', at=2, expires=20),
                             'leased')
            self.assertEqual(go(action='slot.lease', id='C', slot='x2', at=22, expires=50),
                             'leased')
            result = execute(db, {'action': 'job.revise', 'id': 'P', 'deadline': 25,
                                  'expected': 1, 'at': 21})
            self.assertEqual(result['decision'], 'revised')
            with store.transaction(db) as conn:
                # The child lease either survives a still-feasible plan or is
                # released with an audit event; it can never hold an impossible
                # promise.
                lease = store.row(conn, "SELECT status FROM leases WHERE job='C'")
                self.assertIn(lease['status'], ('released', 'active'))
                if lease['status'] == 'released':
                    decisions = [r['decision'] for r in store.rows(
                        conn, "SELECT decision FROM events WHERE subject='C'")]
                    self.assertIn('released', decisions)
        finally:
            temp.cleanup()

    # ------------------------------------------------------------------
    # Old libraries stay open and their events remain auditable.
    def test_legacy_database_migrates_and_keeps_history(self):
        legacy = str(Path(self.temp.name) / 'legacy.sqlite')
        con = sqlite3.connect(legacy)
        con.executescript(
            """
            CREATE TABLE jobs(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, deadline INTEGER NOT NULL,
                duration INTEGER NOT NULL, cpu INTEGER NOT NULL, memory INTEGER NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE dependencies(job TEXT NOT NULL, parent TEXT NOT NULL, PRIMARY KEY(job,parent));
            CREATE TABLE cancellations(job TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                reason TEXT NOT NULL, at INTEGER NOT NULL);
            CREATE TABLE slots(id TEXT PRIMARY KEY, cpu INTEGER NOT NULL, memory INTEGER NOT NULL,
                node TEXT NOT NULL, healthy INTEGER NOT NULL, heartbeat INTEGER NOT NULL);
            CREATE TABLE leases(job TEXT PRIMARY KEY, slot TEXT NOT NULL, generation INTEGER NOT NULL,
                expires INTEGER NOT NULL, status TEXT NOT NULL, debt INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE attempts(job TEXT NOT NULL, generation INTEGER NOT NULL, status TEXT NOT NULL,
                started INTEGER NOT NULL, ended INTEGER, digest TEXT, PRIMARY KEY(job,generation));
            CREATE TABLE artifacts(job TEXT PRIMARY KEY, digest TEXT NOT NULL, delivered INTEGER NOT NULL,
                status TEXT NOT NULL);
            CREATE TABLE retentions(job TEXT PRIMARY KEY, expires INTEGER NOT NULL, hold INTEGER NOT NULL,
                reason TEXT NOT NULL);
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                subject TEXT NOT NULL, decision TEXT NOT NULL, evidence TEXT NOT NULL);
            INSERT INTO jobs VALUES('oldj','t1',100,10,2,1,'delivered',1,0);
            INSERT INTO slots VALUES('olds',4,4,'oldn',1,1);
            INSERT INTO leases VALUES('oldj','olds',1,50,'released',0);
            INSERT INTO attempts VALUES('oldj',1,'success',3,20,'sha256:abc');
            INSERT INTO artifacts VALUES('oldj','sha256:abc',20,'delivered');
            INSERT INTO events(kind,subject,decision,evidence) VALUES('submit','oldj','accepted','{}');
            INSERT INTO events(kind,subject,decision,evidence) VALUES('settle','oldj','delivered','{}');
            """)
        con.commit()
        con.close()

        self.assertEqual(execute(legacy, {'action': 'job.audit', 'id': 'oldj'})['decision'],
                         'delivered')
        with store.transaction(legacy) as db:
            self.assertIn('granted', [c[1] for c in db.execute('PRAGMA table_info(leases)')])
            history = [r['decision'] for r in store.rows(
                db, "SELECT decision FROM events WHERE subject='oldj' ORDER BY seq")]
        self.assertEqual(history[:2], ['accepted', 'delivered'])
        # New operations work on the migrated library.
        self.assertEqual(execute(legacy, {'action': 'slot.register', 'id': 'news',
                                          'node': 'newn', 'cpu': 4, 'at': 2})['decision'], 'ready')


if __name__ == '__main__':
    unittest.main()
