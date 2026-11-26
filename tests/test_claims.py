import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloudctl import store, claims
from cloudctl.__main__ import execute


def digest_of(payload):
    return 'sha256:' + hashlib.sha256(payload.encode()).hexdigest()


class ClaimScenario(unittest.TestCase):
    """Tenant Jia / job J: promised by 100, lease 20..90, heartbeat lost at 50."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'claims.sqlite')
        store.init(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def run_action(self, action, **kw):
        return execute(self.db, {'action': action, **kw})

    dec = lambda self, action, **kw: self.run_action(action, **kw)['decision']

    def dbrow(self, sql, args=()):
        with store.transaction(self.db) as db:
            return store.row(db, sql, args)

    def dbrows(self, sql, args=()):
        with store.transaction(self.db) as db:
            return store.rows(db, sql, args)

    def breached_j(self, jid='J', tenant='jia', child=False, cpu=2):
        self.assertEqual(self.dec('job.submit', id=jid, tenant=tenant,
                                 deadline=100, duration=10, cpu=cpu, created=0),
                         'accepted')
        if child:
            self.assertEqual(self.dec('job.submit', id='K', tenant=tenant,
                                      deadline=150, duration=10, cpu=2,
                                      created=0, parents=[jid]), 'accepted')
        sid = 's-' + jid
        self.assertEqual(self.dec('slot.register', id=sid, node='n-' + jid,
                                 cpu=4, at=1), 'ready')
        self.assertEqual(self.dec('slot.lease', id=jid, slot=sid, at=20,
                                  expires=90), 'leased')
        for t in (30, 40, 49):
            self.assertEqual(self.dec('slot.heartbeat', slot=sid, at=t,
                                      healthy=True), 'healthy')
        self.assertEqual(self.dec('run.start', id=jid, generation=1, at=21),
                         'running')
        # 90 + duration(10) == deadline 100 -> breached, no artifact.
        self.assertEqual(self.dec('run.settle', id=jid, generation=1, at=90,
                                  success=False), 'breached')

    def file_claim(self, cid='C', jid='J', **kw):
        params = dict(id=cid, job=jid, tenant='jia', at=101,
                      contract_version='contract-2025-1', gap_at=50)
        params.update(kw)
        return self.run_action('claim.open', **params)

    def prove_outage(self, cid='C', **kw):
        params = dict(id=cid, evidence_id='fault-dc7', kind='fault_record',
                      at=60, start=50, end=70, source='datacenter',
                      incident_id='DC-7', verified=True)
        params.update(kw)
        return self.run_action('claim.evidence', **params)

    def prove_late_signature(self, cid='C', requested_at=45):
        return self.run_action('claim.evidence', id=cid, evidence_id='sig-late',
                               kind='customer_signature', requested_at=requested_at,
                               at=requested_at, received_at=80,
                               signer='jia-owner', signature='SIG-abcdef',
                               signed=True, verified=True)

    # ------------------------------------------------------------------ basics
    def test_claim_pins_accepted_promise_and_contract(self):
        self.breached_j()
        r = self.file_claim()
        self.assertEqual(r['decision'], 'accepted')
        self.assertEqual(r['pinned_deadline'], 100)
        self.assertEqual(r['job_version'], 1)
        row = self.dbrow('SELECT * FROM claims WHERE id=?', ('C',))
        self.assertEqual(row['deadline'], 100)
        self.assertEqual(row['job_version'], 1)
        self.assertEqual(row['contract_version'], 'contract-2025-1')
        # Same identity replay is idempotent, not a second claim.
        again = self.file_claim()
        self.assertEqual(again['decision'], 'idempotent')
        self.assertEqual(again['decision'], 'already_exists')
        self.assertEqual(
            self.dbrow('SELECT COUNT(*) c FROM claims WHERE id=?', ('C',))['c'], 1)

    def test_changed_deadline_and_stale_receipt_do_not_rewrite_baseline(self):
        # Revise before any attempt: claim pins the then-current version.
        self.assertEqual(self.dec('job.submit', id='J2', tenant='jia',
                                 deadline=100, duration=10, cpu=2, created=0),
                         'accepted')
        self.assertEqual(self.dec('job.revise', id='J2', deadline=110,
                                  expected=1), 'revised')
        self.file_claim(cid='C2', jid='J2', gap_at=50)
        row = self.dbrow('SELECT deadline,job_version FROM claims WHERE id=?',
                         ('C2',))
        self.assertEqual((row['deadline'], row['job_version']), (110, 2))

        # Full J scenario: after the claim pins 100/v1, the protected rerun
        # moves the live promise to 120/v2 but the claim baseline is fixed.
        self.breached_j()
        self.file_claim()
        self.prove_outage()
        self.dec('claim.adjudicate', id='C')
        self.dec('slot.register', id='spare', node='n2', cpu=4, at=100)
        self.run_action('claim.remedy', id='C', kind='rerun', slot='spare',
                        at=101, new_deadline=120, expires=118)
        # An old-generation receipt is stale and creates no delivery.
        self.assertEqual(self.dec('run.settle', id='J', generation=1, at=109,
                                  digest='x'), 'stale_attempt')
        claim = self.dbrow('SELECT deadline,job_version FROM claims WHERE id=?',
                           ('C',))
        job = self.dbrow('SELECT deadline,version FROM jobs WHERE id=?', ('J',))
        self.assertEqual((claim['deadline'], claim['job_version']), (100, 1))
        self.assertEqual((job['deadline'], job['version']), (120, 2))

    def test_heartbeat_gap_alone_is_inconclusive(self):
        self.breached_j()
        self.file_claim()
        r = self.run_action('claim.adjudicate', id='C', at=101)
        self.assertEqual(r['decision'], 'inconclusive')
        self.assertEqual(r['liability'], 'inconclusive')
        # Even the breached stored status is not itself a liability finding.
        self.assertEqual(self.dbrow('SELECT liability FROM claims WHERE id=?',
                                    ('C',))['liability'], 'inconclusive')
        # No remedy may be granted without a ruling.
        blocked = self.run_action('claim.remedy', id='C', kind='credit')
        self.assertEqual(blocked['decision'], 'remedy_blocked')

    def test_late_progress_and_late_heartbeat_do_not_prove_or_double_count(self):
        self.breached_j()
        self.file_claim()
        digest = digest_of('checkpoint-70')
        p1 = self.run_action('claim.evidence', id='C', evidence_id='prog-70',
                             kind='compute_progress', at=70, received_at=70,
                             digest=digest, verifiable=True)
        self.assertEqual(p1['decision'], 'evidence_added')
        r = self.run_action('claim.adjudicate', id='C')
        self.assertEqual(r['decision'], 'inconclusive')

        # The same progress re-uploaded (retry / different id) is not counted.
        p2 = self.run_action('claim.evidence', id='C', evidence_id='prog-70-dup',
                             kind='compute_progress', at=70, received_at=95,
                             digest=digest, verifiable=True)
        self.assertEqual(p2['decision'], 'idempotent')
        versions = self.dbrows(
            'SELECT version FROM claim_evidence WHERE claim=? ORDER BY version',
            ('C',))
        self.assertEqual([v['version'] for v in versions], [1, 2])  # gap + progress

        # A late heartbeat the slot table rejects as stale is still preserved
        # as original evidence, and still proves nothing by itself.
        self.assertEqual(self.dec('slot.heartbeat', slot='s-J', at=80,
                                  healthy=True), 'healthy')
        self.assertEqual(self.dec('slot.heartbeat', slot='s-J', at=70,
                                  healthy=True), 'stale')
        late_hb = self.run_action('claim.evidence', id='C',
                                 evidence_id='hb-late', kind='heartbeat',
                                 at=70, received_at=96,
                                 raw='{"node":"n-J","seq":70}')
        self.assertEqual(late_hb['decision'], 'evidence_added')
        raw = self.dbrow('SELECT raw FROM claim_evidence WHERE eid=?',
                         ('hb-late',))['raw']
        self.assertIn('seq', raw)
        self.assertEqual(self.run_action('claim.adjudicate', id='C')['decision'],
                         'inconclusive')

    # ------------------------------------------------------------------ path A
    def test_platform_outage_path_protected_rerun_delivery_and_successors(self):
        self.breached_j(child=True)
        self.file_claim()
        # Late verifiable progress at 70 still leaves it inconclusive.
        self.run_action('claim.evidence', id='C', evidence_id='p70',
                        kind='progress', at=70, digest=digest_of('p'))
        self.assertEqual(self.run_action('claim.adjudicate', id='C')['decision'],
                         'inconclusive')
        # Datacenter record proves 50..70 outage; customer pause only at 80.
        self.prove_outage()
        self.prove_late_signature(requested_at=80)  # requested after the window
        r = self.run_action('claim.adjudicate', id='C', at=101)
        self.assertEqual(r['decision'], 'platform_liable')
        self.assertEqual(r['decision'], 'platform_fault')
        self.assertEqual(r['liability'], 'platform')

        self.assertEqual(self.dec('slot.register', id='spare', node='n2',
                                  cpu=4, at=100), 'ready')
        rem = self.run_action('claim.remedy', id='C', kind='rerun',
                              slot='spare', at=101, new_deadline=120, expires=118)
        self.assertEqual(rem['decision'], 'rerun_scheduled')
        self.assertEqual(rem['decision'], 'leased')
        self.assertEqual(rem['generation'], 2)
        self.assertTrue(rem['rescheduled'])
        # Affected successor commitment is shifted once, 150 -> 170.
        k = self.dbrow('SELECT deadline,version,status FROM jobs WHERE id=?',
                       ('K',))
        self.assertEqual(k['deadline'], 170)
        self.assertGreaterEqual(k['version'], 2)

        # The rerun lease is protected against preemption.
        self.assertEqual(self.dec('job.submit', id='U', tenant='jia',
                                  deadline=180, duration=5, cpu=1, created=0),
                         'accepted')
        blocked = self.dec('slot.lease', id='U', slot='spare', at=102,
                           expires=110, preempt=True)
        self.assertEqual(blocked, 'lease_protected')

        # Real run through the existing run pipeline produces actual delivery.
        self.assertEqual(self.dec('run.start', id='J', generation=2, at=102),
                         'running')
        self.assertEqual(self.dec('run.settle', id='J', generation=2, at=110,
                                  digest=digest_of('payload'),
                                  content='payload'), 'delivered')
        # Delivered rerun releases its lease and slot reservation exactly once.
        lease = self.dbrow('SELECT status FROM leases WHERE job=?', ('J',))
        self.assertEqual(lease['status'], 'released')
        self.assertEqual(
            self.dbrow("SELECT status FROM slot_reservations WHERE claim=?",
                       ('C',))['status'], 'released')
        remedy = self.dbrow('SELECT status,credit_id FROM remedies WHERE claim=?',
                            ('C',))
        self.assertEqual(remedy['status'], 'fulfilled')
        self.assertIsNone(remedy['credit_id'])  # platform path issues no credit
        self.assertEqual(self.dbrow('SELECT COUNT(*) c FROM credits WHERE claim=?',
                                    ('C',))['c'], 0)
        self.assertEqual(self.dec('job.audit', id='J'), 'delivered')
        # Re-driving the remedy is an idempotent replay, not a second lease.
        again = self.run_action('claim.remedy', id='C', kind='rerun',
                                slot='spare', at=111)
        self.assertEqual(again['decision'], 'fulfilled')
        self.assertEqual(
            self.dbrow("SELECT COUNT(*) c FROM leases WHERE slot='spare' AND status='active'")['c'],
            0)

    # ------------------------------------------------------------------ path B
    def test_customer_signature_late_path_issues_single_transferable_credit(self):
        self.breached_j()
        self.dec('time.purchase', tenant='jia', amount=100)
        before = self.dbrow('SELECT * FROM time_balances WHERE tenant=?',
                            ('jia',))
        self.assertEqual(before['purchased'], 100)

        self.file_claim()
        # A late-arriving signature proving pause was requested before t=50.
        sig = self.prove_late_signature(requested_at=45)
        self.assertEqual(sig['decision'], 'evidence_added')
        r = self.run_action('claim.adjudicate', id='C')
        self.assertEqual(r['decision'], 'customer_liable')
        self.assertEqual(r['decision'], 'customer_fault')
        self.assertEqual(r['liability'], 'customer')

        rem = self.run_action('claim.remedy', id='C', kind='credit',
                              amount=20, at=102)
        self.assertEqual(rem['decision'], 'credit_issued')
        credit_id = rem['credit_id']
        self.assertTrue(credit_id)
        voucher = self.dbrow('SELECT * FROM credits WHERE id=?', (credit_id,))
        self.assertEqual(voucher['status'], 'available')
        self.assertEqual(voucher['transferable'], 1)
        self.assertEqual(voucher['amount'], 20)
        self.assertEqual(voucher['source'], 'purchased_unused_time')
        # Purchased-but-unused time is earmarked exactly once.
        bal = self.dbrow('SELECT * FROM time_balances WHERE tenant=?', ('jia',))
        self.assertEqual(bal['reserved'], 20)
        self.assertEqual(
            self.dbrow('SELECT COUNT(*) c FROM credits WHERE claim=?',
                       ('C',))['c'], 1)
        # J stays undelivered on the customer path.
        self.assertIsNone(self.dbrow('SELECT job FROM artifacts WHERE job=?',
                                     ('J',)))
        self.assertEqual(self.dbrow('SELECT status FROM jobs WHERE id=?',
                                    ('J',))['status'], 'breached')

        # Operator retry / replay cannot issue a second voucher.
        retry = self.run_action('claim.remedy', id='C', kind='credit',
                                amount=20, at=103)
        self.assertEqual(retry['credit_id'], credit_id)
        self.assertTrue(retry['decision'] == 'idempotent'
                        or retry['decision'] == 'credit_issued')
        self.assertEqual(
            self.dbrow('SELECT COUNT(*) c FROM credits WHERE claim=?',
                       ('C',))['c'], 1)

        # Transferability is real: the voucher can move to tenant Yi.
        tr = self.run_action('credit.transfer', credit_id=credit_id,
                             to_tenant='yi', at=200)
        self.assertEqual(tr['decision'], 'credit_transferred')
        self.assertEqual(self.dbrow('SELECT owner FROM credits WHERE id=?',
                                    (credit_id,))['owner'], 'yi')

        # It then affects the next real promise of the new holder.
        self.assertEqual(self.dec('job.submit', id='Y1', tenant='yi',
                                  deadline=300, duration=5, cpu=1, created=201),
                         'accepted')
        red = self.run_action('credit.redeem', credit_id=credit_id, job='Y1',
                              at=202)
        self.assertEqual(red['decision'], 'credit_redeemed')
        self.assertEqual(self.dbrow('SELECT status,redeemed_by_job FROM credits WHERE id=?',
                                    (credit_id,)),
                         {'status': 'redeemed', 'redeemed_by_job': 'Y1'})

    def test_credit_cannot_be_reused_on_a_second_promise(self):
        self.dec('time.purchase', tenant='jia', amount=40)
        self.breached_j()
        self.file_claim()
        self.prove_late_signature()
        self.dec('claim.adjudicate', id='C')
        cid = self.run_action('claim.remedy', id='C', kind='credit',
                              amount=10)['credit_id']
        for j in ('N1', 'N2'):
            self.dec('job.submit', id=j, tenant='jia', deadline=400,
                     duration=1, cpu=1, created=300)
        first = self.run_action('credit.redeem', credit_id=cid, job='N1', at=301)
        self.assertEqual(first['decision'], 'credit_redeemed')
        second = self.run_action('credit.redeem', credit_id=cid, job='N2', at=302)
        self.assertEqual(second['decision'], 'credit_already_used')
        self.assertEqual(second['decision'], 'already_redeemed')
        # Redemption target never moved to N2.
        self.assertEqual(self.dbrow('SELECT redeemed_by_job FROM credits WHERE id=?',
                                    (cid,))['redeemed_by_job'], 'N1')

    # ------------------------------------------------------------------ parent
    def test_parent_undelivered_blocks_platform_remedy_until_parent_delivers(self):
        # Parent P and child C; C has a fresh slot and genuinely breaches.
        self.assertEqual(self.dec('job.submit', id='P', tenant='jia',
                                  deadline=100, duration=10, cpu=2, created=0),
                         'accepted')
        self.assertEqual(self.dec('job.submit', id='C', tenant='jia',
                                   deadline=160, duration=10, cpu=2, created=0,
                                   parents=['P']), 'accepted')
        self.assertEqual(self.dec('slot.register', id='sc', node='nC', cpu=4,
                                  at=140), 'ready')
        self.assertEqual(self.dec('slot.lease', id='C', slot='sc', at=141,
                                  expires=155), 'leased')
        self.assertEqual(self.dec('run.start', id='C', generation=1, at=142),
                         'running')
        self.assertEqual(self.dec('run.settle', id='C', generation=1, at=150,
                                  success=False), 'breached')
        self.file_claim(cid='CC', jid='C')
        self.prove_outage(cid='CC')
        r = self.run_action('claim.adjudicate', id='CC')
        self.assertEqual(r['decision'], 'parent_undelivered')
        self.assertEqual(r['decision'], 'waiting_parents')
        blocked = self.run_action('claim.remedy', id='CC', kind='rerun',
                                  slot='sc', at=151)
        self.assertIn(blocked['decision'],
                      ('parent_undelivered', 'waiting_parents', 'remedy_blocked'))
        self.assertIsNone(self.dbrow('SELECT slot FROM remedies WHERE claim=?',
                                     ('CC',)))

        # Once P is actually delivered, the same evidence supports platform.
        self.dec('slot.register', id='sp', node='nP', cpu=4, at=1)
        self.dec('slot.lease', id='P', slot='sp', at=2, expires=50)
        self.dec('run.start', id='P', generation=1, at=3)
        self.assertEqual(self.dec('run.settle', id='P', generation=1, at=20,
                                  digest=digest_of('parent'), content='parent'),
                         'delivered')
        r = self.run_action('claim.adjudicate', id='CC')
        self.assertEqual(r['decision'], 'platform_liable')

    # ------------------------------------------------------------------ rerun failure
    def test_failed_rerun_frees_slot_and_retry_fulfils_once(self):
        self.breached_j()
        self.file_claim()
        self.prove_outage()
        self.dec('claim.adjudicate', id='C')
        for s in ('spare1', 'spare2'):
            self.dec('slot.register', id=s, node='n-' + s, cpu=4, at=100)
        rem = self.run_action('claim.remedy', id='C', slot='spare1', at=101,
                              new_deadline=120, expires=118)
        self.assertEqual(rem['decision'], 'rerun_scheduled')
        self.assertEqual(rem['generation'], 2)
        self.dec('run.start', id='J', generation=2, at=102)
        # 105+10 < 120 -> retryable failure, not a false delivery.
        self.assertEqual(self.dec('run.settle', id='J', generation=2, at=105,
                                  success=False), 'retryable')
        self.assertEqual(self.dbrow('SELECT status FROM remedies WHERE claim=?',
                                    ('C',))['status'], 'failed')
        self.assertEqual(
            self.dbrow("SELECT status FROM slot_reservations WHERE claim=? AND slot='spare1'",
                       ('C',))['status'], 'released')
        # spare1 has no active lease now.
        self.assertIsNone(self.dbrow(
            "SELECT job FROM leases WHERE slot='spare1' AND status='active'"))

        # Retry on spare2 with a new generation and deliver for real.
        rem2 = self.run_action('claim.remedy', id='C', slot='spare2', at=106,
                               new_deadline=120, expires=118)
        self.assertEqual(rem2['decision'], 'rerun_scheduled')
        self.assertEqual(rem2['generation'], 3)
        self.dec('run.start', id='J', generation=3, at=107)
        self.assertEqual(self.dec('run.settle', id='J', generation=3, at=112,
                                  digest=digest_of('ok'), content='ok'),
                         'delivered')
        row = self.dbrow('SELECT status,slot,lease_generation FROM remedies WHERE claim=?',
                         ('C',))
        self.assertEqual(row['status'], 'fulfilled')
        self.assertEqual(row['slot'], 'spare2')
        self.assertEqual(row['lease_generation'], 3)

    # ------------------------------------------------------------------ timely
    def test_timely_delivery_is_not_a_breach_and_has_no_remedy(self):
        self.assertEqual(self.dec('job.submit', id='D', tenant='jia',
                                  deadline=100, duration=10, cpu=2, created=0),
                         'accepted')
        self.dec('slot.register', id='sd', node='n', cpu=4, at=1)
        self.dec('slot.lease', id='D', slot='sd', at=2, expires=50)
        self.dec('run.start', id='D', generation=1, at=3)
        self.assertEqual(self.dec('run.settle', id='D', generation=1, at=20,
                                  digest=digest_of('p'), content='p'),
                         'delivered')
        self.file_claim(cid='CD', jid='D')
        r = self.run_action('claim.adjudicate', id='CD')
        self.assertEqual(r['decision'], 'not_breached')
        blocked = self.run_action('claim.remedy', id='CD', kind='credit')
        self.assertEqual(blocked['decision'], 'remedy_blocked')
        self.assertEqual(
            self.dbrow('SELECT COUNT(*) c FROM credits WHERE claim=?',
                       ('CD',))['c'], 0)

    # ------------------------------------------------------------------ revoke
    def test_revocation_freezes_claim_and_replay_does_not_resurrect(self):
        self.breached_j()
        self.file_claim()
        self.assertEqual(self.dec('claim.revoke', id='C', at=110,
                                  reason='duplicate'), 'revoked')
        self.assertEqual(self.dec('claim.evidence', id='C', kind='outage',
                                  verified=True), 'revoked')
        self.assertEqual(self.dec('claim.adjudicate', id='C'), 'revoked')
        self.assertEqual(self.dec('claim.remedy', id='C', kind='credit'),
                         'revoked')
        again = self.file_claim()
        self.assertEqual(again['decision'], 'revoked')
        self.assertEqual(self.dbrow('SELECT status FROM claims WHERE id=?',
                                    ('C',))['status'], 'revoked')

    # ------------------------------------------------------------------ concurrency
    def test_two_claims_cannot_occupy_the_same_backup_slot(self):
        for j in ('JA', 'JB'):
            self.breached_j(j)
            self.file_claim(cid='C' + j, jid=j)
            self.prove_outage(cid='C' + j, incident_id='I-' + j)
            self.assertEqual(self.dec('claim.adjudicate', id='C' + j),
                             'platform_liable')
        self.dec('slot.register', id='only-slot', node='nx', cpu=8, at=100)

        barrier = threading.Barrier(2)

        def fire(cid):
            barrier.wait()
            try:
                return execute(self.db, {
                    'action': 'claim.execute', 'id': cid, 'kind': 'rerun',
                    'slot': 'only-slot', 'at': 101,
                    'new_deadline': 120, 'expires': 118})['decision']
            except Exception as exc:  # pragma: no cover - failure path
                return 'EXC:' + type(exc).__name__

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(fire, ['CJA', 'CJB']))

        exceptions = [o for o in outcomes if str(o).startswith('EXC')]
        self.assertEqual(exceptions, [])
        success = [o for o in outcomes if o == 'rerun_scheduled']
        occupied = [o for o in outcomes if o == 'occupied']
        self.assertEqual(len(success), 1, outcomes)
        self.assertEqual(len(occupied), 1, outcomes)
        active = self.dbrows(
            "SELECT job FROM leases WHERE slot='only-slot' AND status='active'")
        self.assertEqual(len(active), 1)
        held = self.dbrows(
            "SELECT claim FROM slot_reservations WHERE slot='only-slot' AND status='held'")
        self.assertEqual(len(held), 1)

    def test_concurrent_credit_redemption_is_single_use(self):
        self.dec('time.purchase', tenant='jia', amount=100)
        self.breached_j()
        self.file_claim()
        self.prove_late_signature()
        self.dec('claim.adjudicate', id='C')
        credit_id = self.run_action('claim.remedy', id='C', kind='credit',
                                    amount=20)['credit_id']
        for i in range(8):
            self.dec('job.submit', id='N%d' % i, tenant='jia', deadline=500 + i,
                     duration=1, cpu=1, created=300)
        barrier = threading.Barrier(8)

        def redeem(i):
            barrier.wait()
            try:
                return execute(self.db, {
                    'action': 'credit.redeem', 'credit_id': credit_id,
                    'job': 'N%d' % i, 'at': 301})['decision']
            except Exception as exc:  # pragma: no cover
                return 'EXC:' + type(exc).__name__

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(redeem, range(8)))
        self.assertEqual([o for o in outcomes if str(o).startswith('EXC')], [])
        self.assertEqual(sum(1 for o in outcomes if o == 'credit_redeemed'), 1,
                         outcomes)
        self.assertEqual(
            self.dbrow("SELECT COUNT(*) c FROM credits WHERE id=? AND status='redeemed'",
                       (credit_id,))['c'], 1)

    def test_concurrent_intake_same_identity_creates_one_claim(self):
        self.breached_j()
        barrier = threading.Barrier(6)

        def open_one(i):
            barrier.wait()
            try:
                return execute(self.db, {'action': 'claim.open', 'id': 'DUP',
                                         'job': 'J', 'at': 101})['decision']
            except Exception as exc:  # pragma: no cover
                return 'EXC:' + type(exc).__name__

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(open_one, range(6)))
        self.assertEqual([o for o in outcomes if str(o).startswith('EXC')], [])
        self.assertEqual(sum(1 for o in outcomes if o == 'accepted'), 1, outcomes)
        self.assertEqual(self.dbrow('SELECT COUNT(*) c FROM claims WHERE id=?',
                                    ('DUP',))['c'], 1)

    # ------------------------------------------------------------------ compat
    def test_fuzzy_action_names_and_direct_memory_calls(self):
        self.breached_j()
        # Alternate prefix / verb spellings resolve.
        r = execute(self.db, {'action': 'appeals.lodge', 'id': 'CA',
                              'job': 'J', 'at': 101, 'gap_at': 50})
        self.assertEqual(r['decision'], 'accepted')
        r = execute(self.db, {'action': 'claims.add-proof', 'id': 'CA',
                              'kind': 'outage', 'start': 50, 'end': 70,
                              'incident_id': 'I', 'verified': True})
        self.assertEqual(r['decision'], 'evidence_added')
        r = execute(self.db, {'action': 'claim.make_ruling', 'id': 'CA'})
        self.assertEqual(r['decision'], 'platform_liable')

        # Direct module usage on a raw ':memory:' connection self-bootstraps.
        mem = sqlite3.connect(':memory:')
        claims.ensure(mem)
        from cloudctl import jobs
        jobs.submit(mem, {'id': 'MJ', 'tenant': 't', 'deadline': 9,
                          'duration': 2, 'cpu': 1, 'created': 0})
        self.assertEqual(claims.file(mem, {'id': 'MC', 'job': 'MJ'})['decision'],
                         'accepted')
        self.assertEqual(claims.decide(mem, {'id': 'MC'})['decision'],
                         'inconclusive')
        mem.commit()
        mem.close()


if __name__ == '__main__':
    unittest.main()
