"""Rotation, emergency revocation and delivery-eligibility acceptance tests.

The two acceptance branches both start from the same single timeout receipt:

* path A  - machine proves its key container never touched leaked material and
  the key service confirms the old mount was reclaimed; the first half of the
  computation survives and resumes at a recovery point on version 5.
* path B  - the machine is offline with no reclamation proof; the lease is
  isolated, temp bytes that read version 4 are condemned, and the job is
  recomputed from scratch on a standby machine with version 5.

Additional scenarios cover duplicate key-service receipts, the offline machine
reconnecting, customer cancellation, a parent job on another tenant delivering
legitimately, standby-machine failure, and two concurrent mount requests.
"""
import hashlib
import os
import tempfile
import unittest

from cloudctl import store, jobs, slots, runs, secret_rotation as sr
from cloudctl import secret_envelope


def digest_of(payload):
    return 'sha256:' + hashlib.sha256(payload.encode()).hexdigest()


class RotationBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, 'state.sqlite')
        store.init(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def db(self):
        return store.transaction(self.db_path)

    def setup_job(self, jid='J', tenant='acme', slot='M', deadline=1000, at=10):
        with self.db() as db:
            jobs.submit(db, {'id': jid, 'tenant': tenant, 'created': 0,
                             'deadline': deadline, 'duration': 10, 'cpu': 2})
            slots.register(db, {'id': slot, 'cpu': 4, 'node': slot + '-node', 'at': at})
            slots.heartbeat(db, slot, at, True)
            slots.lease(db, jid, slot, at + 1, at + 590)

    def heartbeat(self, slot, at):
        with self.db() as db:
            slots.heartbeat(db, slot, at, True)

    def register_v4_and_mount(self, jid='J', slot='M', at=12, generation=1):
        with self.db() as db:
            self.assertEqual(sr.register_envelope(db, jid, 'K', 4, 'sha256:env-v4', at)['decision'],
                             'registered')
            issued = sr.issue_mount(db, jid, generation, at)
            self.assertEqual(issued['decision'], 'issued')
            self.assertEqual(issued['evidence']['version'], 4)
            self.assertEqual(issued['evidence']['slot'], slot)
            self.assertLessEqual(issued['evidence']['ttl'], sr.MOUNT_TTL_CAP)
            self.assertEqual(issued['evidence']['tenant'], 'acme')
            return issued['evidence']['mount_id']

    def start_on(self, jid, generation, at):
        with self.db() as db:
            self.assertEqual(runs.start(db, jid, generation, at)['decision'], 'running')


class AcceptancePathATest(RotationBase):
    """Unexposed container + key-service reclaim: resume on v5 at a recovery point."""

    def test_rotation_resumes_with_evidence_and_delivers_recomputed_chain(self):
        self.setup_job(at=10)
        mount_id = self.register_v4_and_mount(at=12)
        self.start_on('J', 1, 13)
        with self.db() as db:
            # Key service reports K must rotate; locally only a timeout arrives.
            self.assertEqual(sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 14)['decision'], 'rotated')
            timeout = sr.kms_receipt(db, 'rcpt-timeout', 'K', 4, 'timeout', 15,
                                     mount_id=mount_id, slot='M', claim='no_remote_confirmation')
            self.assertEqual(timeout['decision'], 'receipt_recorded')
            self.assertFalse(timeout['evidence']['gate_released'])
        # A timeout must not release the single-writer gate; the old mount is still active.
        with self.db() as db:
            blocked = sr.issue_mount(db, 'J', 1, 16)
            self.assertEqual(blocked['decision'], 'dual_mount_blocked')
            self.assertEqual(blocked['evidence']['active_version'], 4)
        # Revocation of the leaked version 4 freezes the old mount.
        with self.db() as db:
            rev = sr.revoke(db, 'K', 4, 'suspected_leak', 17, tenant='acme')
            self.assertEqual(rev['decision'], 'revoked')
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'revoked_pending')
            self.assertEqual(sr.issue_mount(db, 'J', 1, 18)['decision'], 'awaiting_reclaim')
        # The machine comes back with two independent pieces of evidence.
        with self.db() as db:
            self.assertEqual(sr.attest(db, 'att-M', 'M', 'J', 'K', 4, 'unexposed', 19,
                                       claim='container_measured_no_leak_material')['decision'],
                             'container_unexposed')
            reclaimed = sr.kms_receipt(db, 'rcpt-reclaim-1', 'K', 4, 'reclaim', 20,
                                       mount_id=mount_id, slot='M')
            self.assertEqual(reclaimed['decision'], 'reclaim_confirmed')
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'reclaimed')
        # Both pieces are required; together they allow resuming at a recovery point.
        with self.db() as db:
            resumed = sr.resume_at_recovery_point(db, 'J', 21, digest_of('half-done'))
            self.assertEqual(resumed['decision'], 'issued')
            self.assertEqual(resumed['evidence']['version'], 5)
            self.assertEqual(resumed['evidence']['resumed_from_version'], 4)
            self.assertIn('recovery_id', resumed['evidence'])
            self.assertEqual(resumed['evidence']['attestation_id'], 'att-M')
            recovery_id = resumed['evidence']['recovery_id']
        # Lineage preserves both reads; the v4 read is cured, not silently rewritten.
        with self.db() as db:
            lineage = store.rows(db, "SELECT generation,key_id,version,taint,recovery_id"
                                     " FROM attempt_lineage WHERE job='J' ORDER BY lineage_seq")
            self.assertEqual(sorted((x['key_id'], x['version']) for x in lineage),
                             sorted([('K', 4), ('K', 5)]))
            self.assertEqual({x['recovery_id'] for x in lineage if x['version'] == 5}, {recovery_id})
            self.assertEqual({x['taint'] for x in lineage}, {0})
            verdict = sr.assess_delivery(db, 'J', 22)
            self.assertTrue(verdict['eligible'])
        # Final artifact settles through the normal run flow and carries the chain.
        self.heartbeat('M', 23)
        payload = 'finished-after-recovery'
        with self.db() as db:
            settled = runs.settle(db, 'J', 1, 24, digest_of(payload), True, payload)
            self.assertEqual(settled['decision'], 'delivered')
            artifact = store.row(db, "SELECT * FROM artifacts WHERE job='J'")
            self.assertEqual(artifact['digest'], digest_of(payload))
        with self.db() as db:
            chain = sr.evidence_chain(db, 'J')
            self.assertEqual([e['version'] for e in chain['envelopes']], [4, 5])
            self.assertEqual(chain['revocations'][0]['reason'], 'suspected_leak')
            receipt_actions = {r['action']: r['receipt_id'] for r in chain['kms_receipts']}
            self.assertEqual(receipt_actions.get('timeout'), 'rcpt-timeout')
            self.assertEqual(receipt_actions.get('reclaim'), 'rcpt-reclaim-1')
            self.assertEqual(chain['recovery_points'][0]['point_digest'], digest_of('half-done'))
            self.assertTrue(chain['eligibility']['eligible'])
            # Raw key material must never appear in any local table or event.
            for table in [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
                for cell in db.execute(f"SELECT * FROM {table}"):
                    self.assertNotIn('RAW-SECRET', str(cell))

    def test_recovery_refused_with_only_attestation_or_only_reclaim(self):
        self.setup_job(at=10)
        mount_id = self.register_v4_and_mount(at=12)
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 13)
            sr.revoke(db, 'K', 4, 'suspected_leak', 14, tenant='acme')
            sr.attest(db, 'att-M', 'M', 'J', 'K', 4, 'unexposed', 15)
            missing = sr.resume_at_recovery_point(db, 'J', 16, digest_of('half'))
            self.assertEqual(missing['decision'], 'evidence_incomplete')
            self.assertEqual(missing['evidence']['missing'], ['kms_reclaim_receipt'])
        with self.db() as db:
            self.assertEqual(sr.kms_receipt(db, 'rcpt-reclaim', 'K', 4, 'reclaim', 17,
                                            mount_id=mount_id)['decision'], 'reclaim_confirmed')
            ok = sr.resume_at_recovery_point(db, 'J', 18, digest_of('half'))
            self.assertEqual(ok['decision'], 'issued')


class AcceptancePathBTest(RotationBase):
    """Offline machine, no reclaim proof: quarantine, replan, recompute on standby."""

    def test_offline_machine_quarantines_and_standby_recomputes(self):
        self.setup_job(at=10)
        old_mount = self.register_v4_and_mount(at=12)
        self.start_on('J', 1, 13)
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 14)
            self.assertEqual(sr.kms_receipt(db, 'rcpt-timeout', 'K', 4, 'timeout', 15,
                                            mount_id=old_mount, slot='M')['decision'],
                             'receipt_recorded')
            sr.revoke(db, 'K', 4, 'suspected_leak', 16, tenant='acme')
            # M is offline: no attestation, no reclaim confirmation ever arrives.
            q = sr.isolate_and_replan(db, 'J', 17)
            self.assertEqual(q['decision'], 'quarantined')
            self.assertEqual(store.row(db, "SELECT status FROM leases WHERE job='J'")['status'],
                             'isolated')
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (old_mount,))['status'], 'quarantined')
            self.assertEqual(store.row(db, "SELECT status FROM attempts WHERE job='J' AND generation=1")
                             ['status'], 'quarantined')
        # A late success receipt from the old generation can never deliver tainted bytes.
        with self.db() as db:
            late = runs.settle(db, 'J', 1, 18, digest_of('stale-payload'), True, 'stale-payload')
            self.assertEqual(late['decision'], 'quarantined_attempt_rejected')
            self.assertIsNone(store.row(db, "SELECT * FROM artifacts WHERE job='J'"))
        # Standby machine takes a fresh lease generation; v4 mount stays condemned.
        with self.db() as db:
            slots.register(db, {'id': 'S2', 'cpu': 4, 'node': 'standby-node', 'at': 19})
            slots.heartbeat(db, 'S2', 19, True)
            leased = slots.lease(db, 'J', 'S2', 20, 600)
            self.assertEqual(leased['decision'], 'leased')
            self.assertEqual(leased['evidence']['generation'], 2)
            standby = sr.standby_mount(db, 'J', 2, 21)
            self.assertEqual(standby['decision'], 'issued')
            self.assertEqual(standby['evidence']['version'], 5)
            self.assertEqual(standby['evidence']['slot'], 'S2')
            self.assertEqual(standby['evidence']['generation'], 2)
        # Exactly one active mount exists, and it is the standby's.
        with self.db() as db:
            actives = store.rows(db, "SELECT mount_id,slot,version FROM secret_mounts"
                                     " WHERE job='J' AND status='active'")
            self.assertEqual(len(actives), 1)
            self.assertEqual(actives[0]['slot'], 'S2')
        self.start_on('J', 2, 22)
        # The first settle of the clean generation is blocked: the condemned evidence
        # has not yet been retired by a confirmed clean recomputation.
        self.heartbeat('S2', 25)
        payload = 'fully-recomputed'
        with self.db() as db:
            blocked = runs.settle(db, 'J', 2, 26, digest_of('partial'), True, 'partial')
            self.assertEqual(blocked['decision'], 'secret_delivery_blocked')
            self.assertEqual(store.row(db, "SELECT status FROM artifacts WHERE job='J'")['status'],
                             'quarantined')
            self.assertIsNone(store.row(db, "SELECT * FROM clean_recomputations WHERE job='J'"))
        # Operator verifies the standby recomputation never touched v4 and confirms it.
        with self.db() as db:
            confirm = sr.confirm_clean_recomputation(db, 'J', 2, 27, digest_of(payload))
            self.assertEqual(confirm['decision'], 'confirmed')
            verdict = sr.assess_delivery(db, 'J', 27)
            self.assertTrue(verdict['eligible'])
        # A fresh settle on the clean generation now delivers the recomputed artifact.
        with self.db() as db:
            db.execute("UPDATE attempts SET status='running',digest=NULL,ended=NULL"
                       " WHERE job='J' AND generation=2")
            delivered = runs.settle(db, 'J', 2, 28, digest_of(payload), True, payload)
            self.assertEqual(delivered['decision'], 'delivered')
            artifact = store.row(db, "SELECT * FROM artifacts WHERE job='J'")
            self.assertEqual(artifact['digest'], digest_of(payload))
            self.assertEqual(artifact['status'], 'delivered')
        with self.db() as db:
            chain = sr.evidence_chain(db, 'J')
            self.assertEqual({m['status'] for m in chain['mounts'] if m['version'] == 4},
                             {'quarantined'})
            self.assertTrue(chain['eligibility']['eligible'])
            # Only the timeout receipt exists: the platform never guessed reclaim success.
            self.assertEqual([r['action'] for r in chain['kms_receipts']], ['timeout'])
            # Replayable distinction between the two branches.
            self.assertEqual(chain['recovery_points'], [])


class KmsReceiptAndReconnectTest(RotationBase):
    def test_duplicate_receipts_are_idempotent_and_reconnect_does_not_reopen_mount(self):
        self.setup_job(at=10)
        mount_id = self.register_v4_and_mount(at=12)
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 13)
            sr.revoke(db, 'K', 4, 'suspected_leak', 14, tenant='acme')
            first = sr.kms_receipt(db, 'rcpt-1', 'K', 4, 'reclaim', 15, mount_id=mount_id)
            self.assertEqual(first['decision'], 'reclaim_confirmed')
            second = sr.kms_receipt(db, 'rcpt-1', 'K', 4, 'reclaim', 16, mount_id=mount_id)
            self.assertEqual(second['decision'], 'duplicate_receipt')
            self.assertEqual(store.row(db, "SELECT duplicated FROM kms_revocations WHERE receipt_id='rcpt-1'")
                             ['duplicated'], 1)
            # A distinct receipt with the same reclaim claim is also accepted.
            third = sr.kms_receipt(db, 'rcpt-2', 'K', 4, 'reclaim', 17, mount_id=mount_id)
            self.assertEqual(third['decision'], 'reclaim_confirmed')
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'reclaimed')
        # M reconnects: its old mount stays reclaimed, never reactivated.  The safe v5
        # envelope can now be mounted for a fresh lease generation.
        self.heartbeat('M', 18)
        with self.db() as db:
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'reclaimed')
            # The old generation's lease is still live but its mount is gone.
            self.assertEqual(runs.start(db, 'J', 1, 19)['decision'], 'secret_mount_required')
        # A replacement lease generation may mount the safe version once the gate is clear.
        # A replacement lease generation may mount the safe version once the gate is clear.
        with self.db() as db:
            leased = slots.lease(db, 'J', 'M', 20, 600, preempt=True)
            self.assertEqual(leased['decision'], 'leased')
            self.assertEqual(leased['evidence']['generation'], 2)
        with self.db() as db:
            self.assertEqual(store.row(db, "SELECT generation,status FROM leases WHERE job='J'")
                             ['generation'], 2)
            remount = sr.issue_mount(db, 'J', 2, 21)
            self.assertEqual(remount['decision'], 'issued')
            self.assertEqual(remount['evidence']['version'], 5)
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'reclaimed')


class CustomerCancelConcurrencyTest(RotationBase):
    def test_cancel_rotation_and_receipt_never_leave_two_valid_mounts(self):
        self.setup_job(at=10)
        mount_id = self.register_v4_and_mount(at=12)
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 13)
            # Customer cancellation races the rotation: active mount is closed.
            self.assertEqual(jobs.cancel(db, 'J', 14, 'customer_withdrew')['decision'], 'cancelled')
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (mount_id,))['status'], 'cancelled')
            # A reclaim receipt arriving after cancellation changes nothing deliverable.
            sr.kms_receipt(db, 'rcpt-late', 'K', 4, 'reclaim', 15, mount_id=mount_id)
            self.assertEqual(sr.issue_mount(db, 'J', 1, 16)['decision'], 'unavailable')
            self.assertEqual(store.rows(db, "SELECT * FROM secret_mounts WHERE job='J' AND status='active'"),
                             [])
            intent = store.row(db, "SELECT status,decision FROM rotation_intents WHERE job='J'")
            self.assertEqual(intent['status'], 'closed')
            self.assertEqual(intent['decision'], 'cancelled')
        # Old-generation success receipt after cancel cannot masquerade as v5 output.
        with self.db() as db:
            late = runs.settle(db, 'J', 1, 17, digest_of('late'), True, 'late')
            self.assertEqual(late['decision'], 'stale_attempt')
            self.assertIsNone(store.row(db, "SELECT * FROM artifacts WHERE job='J'"))


class CrossTenantRevocationTest(RotationBase):
    def test_revocation_does_not_travel_across_tenant_dependency_edges(self):
        # Parent belongs to tenant 'vendor' with key KP; child 'J' belongs to 'acme' with K.
        with self.db() as db:
            jobs.submit(db, {'id': 'P', 'tenant': 'vendor', 'created': 0,
                             'deadline': 1000, 'duration': 10, 'cpu': 2})
            jobs.submit(db, {'id': 'J', 'tenant': 'acme', 'created': 0,
                             'deadline': 1000, 'duration': 10, 'cpu': 2, 'parents': ['P']})
            slots.register(db, {'id': 'M', 'cpu': 4, 'node': 'n', 'at': 1})
            slots.heartbeat(db, 'M', 2, True)
            slots.lease(db, 'P', 'M', 3, 600)
            sr.register_envelope(db, 'P', 'KP', 1, 'sha256:env-kp1', 4)
            self.assertEqual(sr.issue_mount(db, 'P', 1, 5)['decision'], 'issued')
        # Revoke the acme key; the vendor parent must remain completely untouched.
        with self.db() as db:
            sr.register_envelope(db, 'J', 'K', 4, 'sha256:env-v4', 6)
            rev = sr.revoke(db, 'K', 4, 'acme_incident', 7, tenant='acme')
            self.assertEqual(rev['evidence']['jobs'], ['J'])
            self.assertEqual(store.row(db, "SELECT status FROM secret_envelope_versions WHERE job='P'")
                             ['status'], 'current')
            self.assertIsNotNone(store.row(db, "SELECT * FROM secret_mounts WHERE job='P' AND status='active'"))
        # Parent still delivers legitimately.
        with self.db() as db:
            runs.start(db, 'P', 1, 8)
            payload = 'vendor-artifact'
            self.assertEqual(runs.settle(db, 'P', 1, 20, digest_of(payload), True, payload)['decision'],
                             'delivered')
        # The reverse: a vendor-side revocation never taints the acme child.
        with self.db() as db:
            sr.revoke(db, 'KP', 1, 'vendor_incident', 30, tenant='vendor')
            self.assertEqual(store.rows(db, "SELECT tenant FROM secret_revocations WHERE key_id='K'"),
                             [{'tenant': 'acme'}])
            self.assertEqual(store.row(db, "SELECT status FROM secret_envelope_versions WHERE job='J'"
                                           " AND key_id='K'")['status'], 'revoked')
            self.assertIsNone(store.row(db, "SELECT id FROM secret_revocations WHERE key_id='K'"
                                            " AND tenant='vendor'"))


class StandbyFailureTest(RotationBase):
    def test_standby_failure_keeps_job_retryable_without_touching_quarantined_mount(self):
        self.setup_job(at=10)
        old_mount = self.register_v4_and_mount(at=12)
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 13)
            sr.kms_receipt(db, 'rcpt-timeout', 'K', 4, 'timeout', 14, mount_id=old_mount, slot='M')
            sr.revoke(db, 'K', 4, 'suspected_leak', 15, tenant='acme')
            sr.isolate_and_replan(db, 'J', 16)
            slots.register(db, {'id': 'S2', 'cpu': 4, 'node': 'standby', 'at': 17})
            slots.heartbeat(db, 'S2', 18, True)
            slots.lease(db, 'J', 'S2', 19, 600)
            self.assertEqual(sr.standby_mount(db, 'J', 2, 20)['decision'], 'issued')
        self.start_on('J', 2, 21)
        with self.db() as db:
            failed = runs.settle(db, 'J', 2, 22, None, False)
            self.assertEqual(failed['decision'], 'retryable')
            self.assertEqual(store.row(db, "SELECT status FROM leases WHERE job='J'")['status'], 'failed')
            # Quarantined v4 mount is never resurrected; no artifact exists.
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (old_mount,))['status'], 'quarantined')
            self.assertIsNone(store.row(db, "SELECT * FROM artifacts WHERE job='J'"))
        # A later healthy standby lease mounts v5 again and finishes the job.
        with self.db() as db:
            slots.heartbeat(db, 'S2', 23, True)
            slots.lease(db, 'J', 'S2', 24, 600)
            again = sr.issue_mount(db, 'J', 3, 25)
            self.assertEqual(again['decision'], 'issued')
            self.assertEqual(again['evidence']['version'], 5)
            runs.start(db, 'J', 3, 26)
            payload = 'recomputed-after-standby-failure'
            # The first clean settle still sees the un-retired condemned generation.
            first = runs.settle(db, 'J', 3, 27, digest_of(payload), True, payload)
            self.assertEqual(first['decision'], 'secret_delivery_blocked')
            # Confirming the safe recomputation retires the quarantine evidence.
            sr.confirm_clean_recomputation(db, 'J', 3, 28, digest_of(payload))
            db.execute("UPDATE attempts SET status='running',digest=NULL,ended=NULL"
                       " WHERE job='J' AND generation=3")
            self.assertEqual(runs.settle(db, 'J', 3, 29, digest_of(payload), True, payload)['decision'],
                             'delivered')
            self.assertEqual(store.row(db, "SELECT key_id,version FROM attempt_lineage"
                                           " WHERE job='J' AND generation=3")['version'], 5)
            # Condemned and failed generations remain replayable evidence.
            # Condemned and failed generations remain replayable evidence.
            attempt_rows = store.rows(db, "SELECT generation,status,digest FROM attempts WHERE job='J'")
            statuses = {r['status'] for r in attempt_rows}
            self.assertIn('quarantined', statuses)
            self.assertIn('failed', statuses)
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE mount_id=?",
                                       (old_mount,))['status'], 'quarantined')
            self.assertEqual(store.row(db, "SELECT taint FROM attempt_lineage"
                                           " WHERE job='J' AND generation=1")['taint'], 1)


class ConcurrentMountTest(RotationBase):
    def test_two_concurrent_mount_requests_cannot_both_hold_write_capability(self):
        self.setup_job(at=10)
        self.register_v4_and_mount(at=12)
        # A second request while the first mount is live is idempotent, not a second mount.
        with self.db() as db:
            self.assertEqual(sr.issue_mount(db, 'J', 1, 13)['decision'], 'idempotent')
        # After rotation the standby request is blocked until the old mount clears.
        with self.db() as db:
            sr.rotate(db, 'J', 'K', 5, 'sha256:env-v5', 14)
            slots.register(db, {'id': 'S2', 'cpu': 4, 'node': 'standby', 'at': 15})
            slots.heartbeat(db, 'S2', 15, True)
            refused = sr.issue_mount(db, 'J', 1, 16)
            self.assertEqual(refused['decision'], 'dual_mount_blocked')
        with self.db() as db:
            # Isolation of M's slot flips the old mount to isolated, releasing the gate.
            slots.heartbeat(db, 'M', 17, False)
            self.assertEqual(store.row(db, "SELECT status FROM secret_mounts WHERE slot='M'")
                             ['status'], 'isolated')
            slots.heartbeat(db, 'S2', 18, True)
            slots.lease(db, 'J', 'S2', 19, 600)
            standby = sr.issue_mount(db, 'J', 2, 20)
            self.assertEqual(standby['decision'], 'issued')
            self.assertEqual(standby['evidence']['slot'], 'S2')
            self.assertEqual([a['slot'] for a in store.rows(
                db, "SELECT slot FROM secret_mounts WHERE job='J' AND status='active'")], ['S2'])
            # No key-service receipt exists: release was justified by lease isolation only.
            self.assertEqual(store.rows(db, "SELECT action FROM kms_revocations"), [])


class MountBindingAndLegacyTest(RotationBase):
    def test_mount_binds_tenant_job_generation_version_and_window(self):
        self.setup_job(at=10)
        with self.db() as db:
            sr.register_envelope(db, 'J', 'K', 4, 'sha256:env-v4', 11)
            issued = sr.issue_mount(db, 'J', 1, 12, ttl=20)
            self.assertEqual(issued['decision'], 'issued')
            ev = issued['evidence']
            self.assertEqual((ev['tenant'], ev['key_id'], ev['version'], ev['generation']),
                             ('acme', 'K', 4, 1))
            self.assertEqual(ev['expires_at'] - ev['issued_at'], ev['ttl'])
            self.assertLessEqual(ev['ttl'], sr.MOUNT_TTL_CAP)
            self.assertEqual(ev['expires_at'], 32)
            # A repeated request inside the window is idempotent, never a second mount.
            self.assertEqual(secret_envelope.mount(db, 'J', 1, 14)['decision'], 'idempotent')
            # Once the short window elapses, run.start loses its secret capability.
            slots.heartbeat(db, 'M', ev['expires_at'], True)
            self.assertEqual(runs.start(db, 'J', 1, ev['expires_at'] + 1)['decision'],
                             'secret_mount_required')
            # Stale generation never receives a mount.
            self.assertEqual(sr.issue_mount(db, 'J', 99, 15)['decision'], 'unavailable')

    def test_short_window_cannot_outlive_the_lease(self):
        self.setup_job(at=10)
        with self.db() as db:
            sr.register_envelope(db, 'J', 'K', 4, 'sha256:env-v4', 11)
            # Lease window ends at 600; request a TTL far beyond it.
            issued = sr.issue_mount(db, 'J', 1, 595, ttl=300)
            self.assertEqual(issued['evidence']['expires_at'], 600)
            self.assertEqual(issued['evidence']['ttl'], 5)


class CliWiringTest(RotationBase):
    def test_full_path_b_flow_through_the_cli_entrypoint(self):
        from cloudctl.__main__ import execute
        path = self.db_path

        def call(**req):
            return execute(path, req)

        self.assertEqual(call(action='job.submit', id='J', tenant='acme', created=0,
                              deadline=1000, duration=10, cpu=2)['decision'], 'accepted')
        self.assertEqual(call(action='slot.register', id='M', node='n1', cpu=4, at=10)['decision'],
                         'ready')
        self.assertEqual(call(action='slot.heartbeat', slot='M', at=10, healthy=True)['decision'],
                         'healthy')
        self.assertEqual(call(action='slot.lease', id='J', slot='M', at=11, expires=600)['decision'],
                         'leased')
        self.assertEqual(call(action='secret.register', id='J', key_id='K', version=4,
                              sealed_digest='sha256:env4', at=12)['decision'], 'registered')
        self.assertEqual(call(action='secret.mount', id='J', generation=1, at=12)['decision'],
                         'issued')
        self.assertEqual(call(action='run.start', id='J', generation=1, at=13)['decision'], 'running')
        self.assertEqual(call(action='secret.rotate', id='J', key_id='K', new_version=5,
                              sealed_digest='sha256:env5', at=14)['decision'], 'rotated')
        self.assertEqual(call(action='secret.kms_receipt', receipt_id='t1', key_id='K', version=4,
                              receipt_action='timeout', at=15, slot='M')['decision'],
                         'receipt_recorded')
        self.assertEqual(call(action='secret.revoke', key_id='K', version=4, reason='leak',
                              at=16, tenant='acme')['decision'], 'revoked')
        self.assertEqual(call(action='secret.quarantine', id='J', at=17)['decision'], 'quarantined')
        self.assertEqual(call(action='slot.register', id='S2', node='n2', cpu=4, at=19)['decision'],
                         'ready')
        self.assertEqual(call(action='slot.heartbeat', slot='S2', at=19, healthy=True)['decision'],
                         'healthy')
        self.assertEqual(call(action='slot.lease', id='J', slot='S2', at=20, expires=600)['decision'],
                         'leased')
        self.assertEqual(call(action='secret.standby_mount', id='J', generation=2, at=21)
                         ['decision'], 'issued')
        self.assertEqual(call(action='run.start', id='J', generation=2, at=22)['decision'], 'running')
        payload = 'cli-recomputed'
        # First clean settle is blocked until the clean recomputation is confirmed.
        blocked = call(action='run.settle', id='J', generation=2, at=26,
                       digest=digest_of(payload), content=payload)
        self.assertEqual(blocked['decision'], 'secret_delivery_blocked')
        self.assertEqual(call(action='secret.confirm_recompute', id='J', generation=2, at=27,
                              digest=digest_of(payload))['decision'], 'confirmed')
        with self.db() as db:
            db.execute("UPDATE attempts SET status='running',digest=NULL,ended=NULL"
                       " WHERE job='J' AND generation=2")
        delivered = call(action='run.settle', id='J', generation=2, at=28,
                         digest=digest_of(payload), content=payload)
        self.assertEqual(delivered['decision'], 'delivered')
        chain = call(action='secret.evidence', id='J')['evidence']
        self.assertEqual([(m['version'], m['status']) for m in chain['mounts']],
                         [(4, 'quarantined'), (5, 'active')])
        self.assertEqual([r['action'] for r in chain['kms_receipts']], ['timeout'])
        self.assertTrue(chain['eligibility']['eligible'])
        # Audit now carries the secret evidence summary.
        audit = call(action='job.audit', id='J')
        self.assertTrue(audit['evidence']['secret']['delivery_eligible'])
        self.assertEqual({v['version'] for v in audit['evidence']['secret']['envelope_versions']},
                         {4, 5})


if __name__ == '__main__':
    unittest.main()
