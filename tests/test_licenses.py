import tempfile
import unittest
import hashlib
from pathlib import Path

from cloudctl import store, licenses
from cloudctl.__main__ import execute
from cloudctl.license_gateway import (
    FakeLicenseService, ServiceUnavailable, register_gateway, reset_gateways,
)

DIGEST = 'sha256:' + hashlib.sha256(b'payload').hexdigest()


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'license.sqlite')
        store.init(self.db)
        reset_gateways()
        self.gateway = register_gateway(self.db, FakeLicenseService())

    def tearDown(self):
        self.temp.cleanup()
        reset_gateways()

    # -- helpers ---------------------------------------------------------------
    def action(self, action, **kw):
        return execute(self.db, {'action': action, **kw})

    def decision(self, action, **kw):
        return self.action(action, **kw)['decision']

    def job(self, jid='J', tenant='acme', **kw):
        request = {'id': jid, 'tenant': tenant, 'deadline': 100, 'duration': 10,
                   'cpu': 2, 'created': 0,
                   'license': {'product': 'solver', 'version': '9.2', 'region': 'cn-north'}}
        request.update(kw)
        return self.decision('job.submit', **request)

    def plain_job(self, jid='J', **kw):
        request = {'id': jid, 'tenant': 'acme', 'deadline': 100, 'duration': 10,
                   'cpu': 2, 'created': 0}
        request.update(kw)
        return self.decision('job.submit', **request)

    def slot(self, sid='S'):
        return self.decision('slot.register', id=sid, node='node-1', cpu=4, at=1)

    def lease(self, jid='J', sid='S', generation=1, at=2, expires=50):
        return self.decision('slot.lease', id=jid, slot=sid, at=at, expires=expires)

    def byol(self, eid='C', tenant='acme', seats=1, version='9.2', region='cn-north',
             start=0, end=200):
        return self.decision('license.entitlement', id=eid, product='solver',
                             version=version, region=region, tenant=tenant,
                             kind='byol', seats=seats, borrowed_from=start,
                             borrowed_until=end)

    def backup(self, eid='O', seats=2, region='cn-north'):
        return self.decision('license.entitlement', id=eid, product='solver',
                             version='9.2', region=region, kind='operator',
                             seats=seats, borrowed_from=0, borrowed_until=200)

    def borrow(self, jid='J', generation=1, at=3, until=50, **kw):
        return self.action('license.borrow', id=jid, generation=generation,
                           at=at, until=until, **kw)

    def start(self, jid='J', generation=1, at=4):
        return self.decision('run.start', id=jid, generation=generation, at=at)

    def deliver(self, jid='J', generation=1, at=20):
        return self.decision('run.settle', id=jid, generation=generation, at=at,
                             digest=DIGEST, content='payload')

    def grants(self, jid='J'):
        with store.transaction(self.db) as db:
            return store.rows(db, 'SELECT * FROM grants WHERE job=? ORDER BY id', (jid,))

    def grant(self, jid='J', generation=1):
        with store.transaction(self.db) as db:
            return store.row(db, 'SELECT * FROM grants WHERE id=?',
                             (f'grant-{jid}-{generation}',))

    def receipts(self):
        with store.transaction(self.db) as db:
            return store.rows(db, 'SELECT * FROM license_receipts ORDER BY seq')

    def corrections(self):
        with store.transaction(self.db) as db:
            return store.rows(db, 'SELECT * FROM license_corrections ORDER BY seq')

    # ======================================================================
    # Day-J narrative: same local pending record, two lawful resolutions
    # ======================================================================
    def _day_j_setup(self):
        self.job()
        self.slot()
        self.lease()
        self.byol(seats=1)
        self.backup(seats=2)
        # An old attempt on C's last seat whose return confirmation is lost.
        self.job('old-job')
        self.decision('slot.register', id='S-old', node='node-2', cpu=4, at=1)
        self.decision('slot.lease', id='old-job', slot='S-old', at=2, expires=40)
        old = self.borrow('old-job', 1, at=3)
        self.assertEqual(old['decision'], 'borrowed')
        old_token = old['evidence']['token']
        self.gateway.vendor_reclaim(old_token, at=5, lose_confirmation=True)
        # Local view: C's last seat still looks occupied; its receipt is pending.
        with store.transaction(self.db) as db:
            local_used = len(store.rows(db, "SELECT id FROM grants WHERE entitlement='C' AND status IN ('held','return_pending','quarantined','emergency')"))
        self.assertEqual(local_used, 1)
        return old_token

    def test_day_j_path_a_byol_verified_customer_pays(self):
        old_token = self._day_j_setup()
        # Vendor reachable: reconcile and learn the seat was actually reclaimed.
        rec = self.action('license.reconcile', id='old-job', generation=1, at=6)
        self.assertEqual(rec['decision'], 'reclaimed_by_vendor')
        # Now J borrows a NEW token under C; responsibility stays with customer.
        b = self.borrow()
        self.assertEqual(b['decision'], 'borrowed')
        self.assertEqual(b['evidence']['entitlement'], 'C')
        self.assertNotEqual(b['evidence']['token'], old_token)
        self.assertEqual(b['evidence']['responsibility'], 'customer')
        self.assertEqual(self.start(), 'running')
        self.assertEqual(self.deliver(), 'delivered')
        g = self.grant('J', 1)
        self.assertEqual(g['status'], 'returned')
        ev = self.action('license.audit', id='J')
        self.assertEqual(ev['decision'], 'compliant')
        self.assertEqual(ev['evidence']['responsibility'], 'customer')
        self.assertEqual(ev['evidence']['delivery_evidence']['token'],
                         b['evidence']['token'])
        # Operator backup O was never touched.
        snap = self.gateway.snapshot()
        self.assertEqual(snap['active_count'] if False else
                         sum(1 for t in snap['tokens'].values()
                             if t['entitlement'] == 'O' and t['status'] == 'held'), 0)

    def test_day_j_path_b_operator_backup_when_c_unverifiable(self):
        self._day_j_setup()
        self.gateway.set_offline(True)
        # Cannot verify C while the vendor is down.
        rec = self.action('license.reconcile', id='old-job', generation=1, at=6)
        self.assertEqual(rec['decision'], 'service_unavailable')
        # Customer pre-agreed: operator lends from O's reserved seats;
        # the operator pays rent and C's old hold stays for separate write-off.
        self.gateway.set_offline(False)
        b = self.borrow()
        self.assertEqual(b['decision'], 'borrowed')
        self.assertEqual(b['evidence']['entitlement'], 'O')
        self.assertEqual(b['evidence']['responsibility'], 'operator')
        self.assertEqual(self.start(), 'running')
        self.assertEqual(self.deliver(), 'delivered')
        ev = self.action('license.audit', id='J')
        self.assertEqual(ev['decision'], 'compliant')
        self.assertEqual(ev['evidence']['responsibility'], 'operator')
        # C's old occupancy is still open, never silently erased.
        old = self.grant('old-job', 1)
        self.assertEqual(old['status'], 'held')
        # Later vendor comes back, confirms the old C token was reclaimed.
        r = self.action('license.reconcile', id='old-job', generation=1, at=30)
        self.assertEqual(r['decision'], 'reclaimed_by_vendor')
        self.assertEqual(self.grant('old-job', 1)['status'], 'returned')

    def test_cannot_claim_both_contracts_and_erase_one_ledger(self):
        self._day_j_setup()
        self.gateway.set_offline(True)
        # An attempted borrow while offline creates at most one quarantined
        # pending record; it never fans out across both contracts.
        b = self.borrow()
        self.assertEqual(b['decision'], 'service_unavailable_pending')
        pending = self.grants('J')
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['status'], 'quarantined')
        self.gateway.set_offline(False)
        # Re-borrow attempt on the same generation cannot start a second grant;
        # the pending one must be reconciled or written off first.
        again = self.borrow()
        self.assertEqual(again['decision'], 'grant_not_reusable')
        with store.transaction(self.db) as db:
            self.assertEqual(len(store.rows(db, 'SELECT * FROM grants WHERE job=?', ('J',))), 1)
        # Immutable history: receipts/corrections are append-only.
        before = len(self.receipts())
        self.decision('license.writeoff', id='J', generation=1, at=40,
                      correction_id='wo-1', reason='manual review', operator='ops')
        after = self.receipts()
        self.assertEqual(len(after), before)  # write-off adds a correction, not a delete
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.grant('J', 1)['status'], 'cancelled')

    # ======================================================================
    # Entitlement validation: version, region, tenant, windows
    # ======================================================================
    def test_version_mismatch_rejects_borrow(self):
        self.job()
        self.slot()
        self.lease()
        self.byol(version='8.1')  # job requires 9.2
        self.assertEqual(self.borrow()['decision'], 'no_entitlement')

    def test_region_migration_requires_matching_region(self):
        self.job()
        self.slot()
        self.lease()
        self.byol(region='eu-west')
        self.backup(region='eu-west')
        self.assertEqual(self.borrow()['decision'], 'no_entitlement')
        # A region-matched entitlement makes the borrow legal.
        self.byol(eid='C2', region='cn-north')
        b = self.borrow()
        self.assertEqual(b['decision'], 'borrowed')
        self.assertEqual(b['evidence']['entitlement'], 'C2')

    def test_byol_seats_cannot_serve_another_tenant(self):
        self.job(tenant='acme')
        self.slot()
        self.lease()
        self.byol(tenant='rival')  # someone else's customer-owned license
        self.backup()
        b = self.borrow()
        # Rival's C is invisible to acme; only operator backup can serve.
        self.assertEqual(b['evidence']['entitlement'], 'O')
        self.assertEqual(b['evidence']['responsibility'], 'operator')

    def test_customer_contract_preferred_over_operator_backup(self):
        self.job()
        self.slot()
        self.lease()
        self.backup()
        self.byol()
        b = self.borrow()
        self.assertEqual(b['evidence']['entitlement'], 'C')
        self.assertEqual(b['evidence']['responsibility'], 'customer')

    def test_attempt_crossing_two_windows_needs_full_coverage_or_reapproval(self):
        self.job()
        self.slot()
        self.lease()
        # Two adjacent windows with a gap: [0,20) and [25,60); no single
        # right continuously covers a span that crosses the gap.
        self.decision('license.entitlement', id='C', product='solver',
                      version='9.2', region='cn-north', tenant='acme',
                      kind='byol', seats=1, borrowed_from=0, borrowed_until=60,
                      windows=[{'start': 0, 'end': 20}, {'start': 25, 'end': 60}])
        self.assertEqual(self.borrow(at=3, until=50)['decision'], 'no_entitlement')
        # A borrow ending inside the first window is legal.
        b = self.borrow(at=3, until=18)
        self.assertEqual(b['decision'], 'borrowed')
        # The window ends at 20; at t=21 the same right no longer covers the
        # attempt even though the vendor token still exists.
        self.assertEqual(self.start(at=21), 'window_boundary_reauthorization_required')
        # Re-approval against the second window restores legality and extends
        # the grant's approved lifetime.
        r = self.action('license.reauthorize', id='J', generation=1, at=26, until=45)
        self.assertEqual(r['decision'], 'reauthorized_same_right')
        self.assertEqual(self.grant('J', 1)['expires_at'], 45)
        self.assertEqual(self.start(at=27), 'running')

    # ======================================================================
    # Concurrency: two jobs of the same tenant competing for the last seat
    # ======================================================================
    def test_two_jobs_competing_for_last_seat(self):
        self.job('j1')
        self.job('j2')
        self.slot()
        self.lease('j1')
        self.decision('slot.register', id='S2', node='n2', cpu=4, at=2)
        self.decision('slot.lease', id='j2', slot='S2', at=2, expires=50)
        self.byol(seats=1)
        b1 = self.borrow('j1', 1, at=3)
        self.assertEqual(b1['decision'], 'borrowed')
        b2 = self.borrow('j2', 1, at=3)
        self.assertEqual(b2['decision'], 'no_seat_available')
        # Replaying j1's borrow never double-charges: same request id, same token.
        replay = self.borrow('j1', 1, at=3)
        self.assertEqual(replay['decision'], 'idempotent')
        self.assertEqual(replay['evidence']['token'], b1['evidence']['token'])
        snap = self.gateway.snapshot()
        self.assertEqual(sum(1 for t in snap['tokens'].values()
                             if t['entitlement'] == 'C'), 1)
        # Once j1 finishes and returns the seat, j2 can borrow it.
        self.assertEqual(self.start('j1', at=4), 'running')
        self.assertEqual(self.deliver('j1', at=10), 'delivered')
        self.assertEqual(self.grant('j1', 1)['status'], 'returned')
        b2b = self.borrow('j2', 1, at=11)
        self.assertEqual(b2b['decision'], 'borrowed')
        self.assertNotEqual(b2b['evidence']['token'], b1['evidence']['token'])

    # ======================================================================
    # Lease preemption releases software seats; new generation re-borrows
    # ======================================================================
    def test_lease_preemption_returns_seat_and_requires_new_borrow(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        self.assertEqual(b['decision'], 'borrowed')
        self.assertEqual(self.start(), 'running')
        # Preempt J with an urgent job.
        self.plain_job('urgent')
        self.assertEqual(self.decision('slot.lease', id='urgent', slot='S',
                                       at=5, expires=45, preempt=True), 'leased')
        # J's seat was checked in as part of preemption.
        self.assertEqual(self.grant('J', 1)['status'], 'returned')
        # J gets a fresh lease generation; the old grant cannot be reused.
        self.decision('slot.register', id='S2', node='n2', cpu=4, at=6)
        self.assertEqual(self.decision('slot.repay', id='J', slot='S2', at=7),
                         'repaid')
        self.assertEqual(self.start(generation=2, at=8), 'unlicensed')
        b2 = self.borrow(generation=2, at=8)
        self.assertEqual(b2['decision'], 'borrowed')
        self.assertNotEqual(b2['evidence']['token'], b['evidence']['token'])
        self.assertEqual(self.start(generation=2, at=9), 'running')

    # ======================================================================
    # Cancellation with a return that times out at the vendor
    # ======================================================================
    def test_cancel_return_timeout_keeps_immutable_correction(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.assertEqual(self.borrow()['decision'], 'borrowed')
        self.assertEqual(self.start(), 'running')
        self.gateway.set_offline(True)
        c = self.action('job.cancel', id='J', at=10, reason='user')
        self.assertEqual(c['decision'], 'cancelled')
        g = self.grant('J', 1)
        self.assertEqual(g['status'], 'return_pending')
        # Default lawful timeout is a fixed grace window after the checkin.
        self.assertEqual(g['return_until'], 10 + 300)
        # Timeout not reached yet.
        early = self.action('license.timeout', id='J', generation=1, at=11)
        self.assertEqual(early['decision'], 'timeout_not_reached')
        # After the lawful timeout the seat expires locally, with a correction.
        late = self.action('license.timeout', id='J', generation=1, at=400)
        self.assertEqual(late['decision'], 'timed_out')
        self.assertEqual(self.grant('J', 1)['status'], 'timed_out')
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.corrections()[0]['kind'], 'return_timeout')
        # A late vendor release receipt cannot resurrect or rewrite the grant.
        self.gateway.set_offline(False)
        token = g['token']
        late_rcp = self.action('license.receipt', at=401,
                               receipt={'receipt_id': 'late-1', 'op': 'release',
                                        'status': 'released', 'token': token})
        self.assertEqual(late_rcp['decision'], 'late_receipt_for_timed_out')
        marked = [r for r in self.receipts() if r['receipt_id'] == 'late-1'][0]
        self.assertEqual(marked['stale'], 1)
        self.assertEqual(self.grant('J', 1)['status'], 'timed_out')

    # ======================================================================
    # Old-generation success receipts cannot re-legalise a new attempt
    # ======================================================================
    def test_stale_generation_receipt_is_ignored(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b1 = self.borrow(generation=1)
        self.assertEqual(b1['decision'], 'borrowed')
        self.assertEqual(self.start(generation=1, at=4), 'running')
        # Generation 1 fails; its seat is returned and a generation-2 lease
        # replaces it (e.g. node crash plus repayment on the same slot).
        self.assertEqual(self.decision('run.settle', id='J', generation=1,
                                       at=8, success=False), 'retryable')
        self.assertEqual(self.grant('J', 1)['status'], 'returned')
        self.decision('slot.lease', id='J', slot='S', at=10, expires=50,
                      preempt=True)
        b2 = self.borrow(generation=2, at=11)
        self.assertEqual(b2['decision'], 'borrowed')
        # A delayed borrow receipt for the old generation arrives after its
        # grant has already been returned: it cannot re-legalise the attempt.
        stale = self.action('license.receipt', at=12,
                            receipt={'receipt_id': 'stale-borrow', 'op': 'borrow',
                                     'status': 'granted', 'token': b1['evidence']['token'],
                                     'generation': 1, 'job': 'J'})
        self.assertEqual(stale['decision'], 'late_borrow_cannot_relegalize')
        # A receipt mis-tagged with the new generation is rejected as stale.
        mistag = self.action('license.receipt', at=13,
                             receipt={'receipt_id': 'stale-borrow-2', 'op': 'borrow',
                                      'status': 'granted', 'token': b1['evidence']['token'],
                                      'generation': 2, 'job': 'J'})
        self.assertEqual(mistag['decision'], 'stale_generation_rejected')
        row = [r for r in self.receipts() if r['receipt_id'] == 'stale-borrow'][0]
        self.assertEqual(row['stale'], 1)
        # Generation 2 remains the only legal seat for the running attempt.
        self.assertEqual(self.start(generation=2, at=13), 'running')
        self.assertEqual(self.deliver(generation=2, at=19), 'delivered')

    # ======================================================================
    # Double start / power-loss replay: idempotency end to end
    # ======================================================================
    def test_double_start_and_replay_do_not_double_charge(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        self.assertEqual(self.start(), 'running')
        # Double start after crash/replay.
        self.assertEqual(self.start(), 'idempotent')
        # Replay of the whole borrow request returns the same token.
        again = self.borrow()
        self.assertEqual(again['evidence']['token'], b['evidence']['token'])
        snap = self.gateway.snapshot()
        self.assertEqual(sum(1 for t in snap['tokens'].values()
                             if t['entitlement'] == 'C' and t['status'] == 'held'), 1)
        with store.transaction(self.db) as db:
            seat = store.row(db, 'SELECT * FROM attempt_seats WHERE job=? AND generation=?',
                             ('J', 1))
        self.assertEqual(seat['token'], b['evidence']['token'])

    def test_borrow_response_lost_leaves_quarantine_not_reborrow(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.gateway.queue_fault(lambda s, m, k: m == 'borrow')
        b = self.borrow()
        self.assertEqual(b['decision'], 'service_unavailable_pending')
        g = self.grant('J', 1)
        self.assertEqual(g['status'], 'quarantined')
        self.assertIsNone(g['token'])
        # Vendor may actually hold a seat: local capacity stays conservative.
        self.assertEqual(self.borrow()['decision'], 'grant_not_reusable')
        # Start is refused while the seat is unverifiable.
        self.assertEqual(self.start(), 'unlicensed')
        # Quarantine cannot be silently timed out; only audited write-off.
        self.assertEqual(self.action('license.timeout', id='J', generation=1,
                                     at=999)['decision'], 'cannot_timeout_quarantine')
        self.assertEqual(self.decision('license.writeoff', id='J', generation=1,
                                       at=1000, correction_id='wo-q',
                                       reason='vendor confirmed no hold',
                                       operator='ops'), 'written_off')

    # ======================================================================
    # Vendor post-hoc correction: duplicate charge refund
    # ======================================================================
    def test_vendor_double_charge_correction_is_immutable(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        token = b['evidence']['token']
        correction = {'correction_id': 'corr-1', 'kind': 'double_charge_refund',
                      'token': token, 'reference': b['evidence']['receipt_id']}
        r = self.action('license.correction', at=30, correction=correction)
        self.assertEqual(r['decision'], 'double_charge_quarantined')
        self.assertEqual(self.grant('J', 1)['status'], 'quarantined')
        # Original borrow receipt remains in place and unaltered.
        borrow_receipts = [x for x in self.receipts() if x['op'] == 'borrow']
        self.assertEqual(len(borrow_receipts), 1)
        self.assertEqual(borrow_receipts[0]['status'], 'granted')
        # Replaying the correction is idempotent.
        self.assertEqual(self.action('license.correction', at=31,
                                     correction=correction)['decision'], 'idempotent')
        self.assertEqual(len(self.corrections()), 1)

    # ======================================================================
    # Manual emergency borrow on operator backup
    # ======================================================================
    def test_emergency_borrow_uses_operator_reserve_and_reconciles(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.backup()
        self.gateway.set_offline(True)
        r = self.action('license.emergency', id='J', generation=1, at=3,
                        reason='vendor outage, deadline risk', operator='ops-1')
        self.assertEqual(r['decision'], 'emergency_borrowed')
        self.assertEqual(r['evidence']['responsibility'], 'operator')
        self.assertEqual(r['evidence']['entitlement'], 'O')
        g = self.grant('J', 1)
        self.assertEqual(g['status'], 'emergency')
        # Emergency grant is enough to run.
        self.assertEqual(self.start(), 'running')
        self.assertEqual(self.deliver(), 'delivered')
        self.assertEqual(len(self.corrections()), 1)
        self.assertEqual(self.corrections()[0]['kind'], 'emergency_borrow')
        ev = self.action('license.audit', id='J')
        self.assertEqual(ev['decision'], 'compliant')
        self.assertEqual(ev['evidence']['responsibility'], 'operator')

    def test_emergency_borrow_cannot_touch_customer_byol(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()  # only customer contract, no operator backup
        self.gateway.set_offline(True)
        r = self.action('license.emergency', id='J', generation=1, at=3,
                        reason='outage', operator='ops-1')
        self.assertEqual(r['decision'], 'no_operator_backup')

    # ======================================================================
    # Parent licenses do not authorise children; delivery evidence linkage
    # ======================================================================
    def test_parent_grant_does_not_authorise_child(self):
        self.job('parent')
        self.job('child', parents=['parent'], deadline=200)
        self.slot()
        self.decision('slot.register', id='S2', node='n2', cpu=4, at=2)
        self.lease('parent', 'S')
        self.decision('slot.lease', id='child', slot='S2', at=2, expires=90)
        self.byol(seats=2)
        self.assertEqual(self.borrow('parent', 1)['decision'], 'borrowed')
        # Child has its own requirement row but no grant of its own.
        self.assertEqual(self.start('child', generation=1, at=5), 'unlicensed')
        self.assertEqual(self.borrow('child', 1, at=5)['decision'], 'borrowed')
        self.assertEqual(self.start('child', generation=1, at=6), 'running')

    def test_delivery_evidence_points_back_to_token_and_receipt(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        self.start()
        self.deliver()
        with store.transaction(self.db) as db:
            ev = store.row(db, 'SELECT * FROM delivery_evidence WHERE job=?', ('J',))
        self.assertEqual(ev['grant'], f'grant-J-1')
        self.assertEqual(ev['token'], b['evidence']['token'])
        self.assertEqual(ev['receipt_id'], b['evidence']['receipt_id'])
        audit = self.action('job.audit', id='J')
        self.assertEqual(audit['evidence']['license_evidence']['grant'], 'grant-J-1')
        self.assertEqual(audit['evidence']['license_evidence']['token'],
                         b['evidence']['token'])

    def test_job_without_license_requirement_runs_as_before(self):
        # Backwards compatibility: no software requirement, no seat needed.
        self.plain_job()
        self.slot()
        self.lease()
        self.assertEqual(self.start(), 'running')
        self.assertEqual(self.deliver(), 'delivered')
        self.assertEqual(self.action('license.audit', id='J')['decision'],
                         'no_license_required')

    def test_local_row_delete_does_not_release_vendor_seat(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        self.start()
        # Tamper: someone deletes the local grant row.
        with store.transaction(self.db) as db:
            db.execute('DELETE FROM grants WHERE id=?', (f'grant-J-1',))
        # The vendor still holds the token.
        verdict = self.gateway.verify('C', b['evidence']['token'])
        self.assertEqual(verdict['status'], 'held')
        # And the running attempt can no longer prove a seat, so delivery is
        # refused even though the attempt row still exists.
        self.assertEqual(self.deliver(), 'unlicensed')
        # The vendor-side release must go through the gateway with the token.
        rcp = self.gateway.release(b['evidence']['token'], 'manual-cleanup', 30)
        self.assertEqual(rcp['status'], 'released')
        self.assertEqual(self.gateway.verify('C', b['evidence']['token'])['status'],
                         'released')

    def test_cancelled_entitlement_cannot_lend_more_seats(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.assertEqual(self.decision('license.entitlement.cancel',
                                       id='C', at=2, reason='contract end'),
                         'cancelled')
        self.assertEqual(self.borrow()['decision'], 'no_entitlement')

    def test_checkin_idempotent_and_unknown_token_receipts_are_kept(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.assertEqual(self.borrow()['decision'], 'borrowed')
        first = self.action('license.checkin', id='J', generation=1, at=10)
        self.assertEqual(first['decision'], 'returned')
        second = self.action('license.checkin', id='J', generation=1, at=11)
        self.assertEqual(second['decision'], 'idempotent')
        # An orphan receipt for a token nobody locally knows is quarantined,
        # never silently applied.
        orphan = self.action('license.receipt', at=12,
                             receipt={'receipt_id': 'orp-1', 'op': 'release',
                                      'status': 'released', 'token': 'tok-ghost'})
        self.assertEqual(orphan['decision'], 'orphan_receipt_quarantined')
        self.assertTrue([r for r in self.receipts() if r['receipt_id'] == 'orp-1'][0]['stale'])

    def test_adjacent_windows_without_gap_cover_one_attempt(self):
        self.job()
        self.slot()
        self.lease()
        self.decision('license.entitlement', id='C', product='solver',
                      version='9.2', region='cn-north', tenant='acme',
                      kind='byol', seats=1, borrowed_from=0, borrowed_until=60,
                      windows=[{'start': 0, 'end': 25}, {'start': 25, 'end': 60}])
        b = self.borrow(at=3, until=50)
        self.assertEqual(b['decision'], 'borrowed')
        with store.transaction(self.db) as db:
            covered = store.rows(db, 'SELECT * FROM grant_windows WHERE grant=?',
                                 ('grant-J-1',))
        self.assertEqual(len(covered), 2)
        self.assertEqual(self.start(at=30), 'running')

    def test_vendor_rejection_falls_through_to_operator_backup(self):
        self.job()
        self.slot()
        self.lease()
        self.byol(seats=1)
        self.backup(seats=1)
        # Exhaust C at the vendor itself so the vendor rejects the borrow.
        self.gateway.tokens['tok-foreign'] = {
            'entitlement': 'C', 'job': 'other', 'generation': 1,
            'request_id': 'foreign', 'status': 'held', 'granted_at': 0}
        b = self.borrow()
        self.assertEqual(b['decision'], 'borrowed')
        self.assertEqual(b['evidence']['entitlement'], 'O')
        self.assertEqual(b['evidence']['responsibility'], 'operator')
        # The rejection receipt is still on record.
        rejected = [r for r in self.receipts() if r['status'] == 'rejected']
        self.assertEqual(len(rejected), 1)

    def test_compliance_audit_flags_quarantine_and_missing_evidence(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        # Quarantined grant makes the report non-compliant.
        self.gateway.queue_fault(lambda s, m, k: m == 'borrow')
        self.assertEqual(self.borrow()['decision'], 'service_unavailable_pending')
        report = self.action('license.audit', id='J')
        self.assertEqual(report['decision'], 'quarantined_seats')
        # No grant at all for a job that needs one.
        self.job('j2')
        self.decision('slot.register', id='S2', node='n2', cpu=4, at=1)
        self.decision('slot.lease', id='j2', slot='S2', at=2, expires=50)
        self.assertEqual(self.action('license.audit', id='j2')['decision'], 'no_grant')

    def test_grant_responsibility_is_recorded_per_contract(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        self.backup()
        b = self.borrow()
        g = self.grant('J', 1)
        self.assertEqual(g['responsibility'], 'customer')
        self.assertEqual(g['tenant'], 'acme')
        self.assertEqual(g['entitlement'], b['evidence']['entitlement'])

    def test_cancelled_contract_rejects_late_receipts(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b = self.borrow()
        token = b['evidence']['token']
        # Deliver and return, then the contract is cancelled.
        self.assertEqual(self.start(), 'running')
        self.assertEqual(self.deliver(), 'delivered')
        self.assertEqual(self.decision('license.entitlement.cancel',
                                       id='C', at=25, reason='contract end'),
                         'cancelled')
        self.assertEqual(self.grant('J', 1)['remote_cancelled'], 1)
        # A late vendor receipt against the old token cannot re-legalise it.
        r = self.action('license.receipt', at=30,
                        receipt={'receipt_id': 'late-c', 'op': 'borrow',
                                 'status': 'granted', 'token': token,
                                 'generation': 1})
        self.assertEqual(r['decision'], 'receipt_for_cancelled_entitlement')
        self.assertTrue([x for x in self.receipts()
                         if x['receipt_id'] == 'late-c'][0]['stale'])

    def test_attempt_seat_binding_is_generation_scoped(self):
        self.job()
        self.slot()
        self.lease()
        self.byol()
        b1 = self.borrow(generation=1)
        self.start(generation=1)
        with store.transaction(self.db) as db:
            seat1 = store.row(db, 'SELECT * FROM attempt_seats WHERE job=? AND generation=?',
                              ('J', 1))
        self.assertEqual(seat1['token'], b1['evidence']['token'])
        # Fail, re-lease, re-borrow on generation 2: distinct token binding.
        self.decision('run.settle', id='J', generation=1, at=8, success=False)
        self.decision('slot.lease', id='J', slot='S', at=10, expires=50,
                      preempt=True)
        b2 = self.borrow(generation=2, at=11)
        self.assertNotEqual(b2['evidence']['token'], b1['evidence']['token'])
        self.start(generation=2, at=12)
        with store.transaction(self.db) as db:
            seats = store.rows(db, 'SELECT * FROM attempt_seats WHERE job=? ORDER BY generation',
                               ('J',))
        self.assertEqual(len(seats), 2)
        self.assertEqual(seats[1]['grant'], 'grant-J-2')
        self.assertEqual(seats[1]['token'], b2['evidence']['token'])


if __name__ == '__main__':
    unittest.main()
