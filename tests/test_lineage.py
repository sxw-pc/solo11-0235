"""Lineage-proof retention: children stay auditable after their parents' bytes go.

Each test uses a fresh temporary database and the project's own CLI dispatcher
(``execute``), so it exercises the same code paths as the fixtures.
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cloudctl import lineage, store
from cloudctl.__main__ import execute


def digest(content):
    return 'sha256:' + hashlib.sha256(content.encode()).hexdigest()


class LineageBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'lineage.sqlite')
        store.init(self.db)
        self._cache = {}

    def tearDown(self):
        self.temp.cleanup()

    def act(self, action, **kw):
        return execute(self.db, {'action': action, **kw})['decision']

    def result(self, action, **kw):
        return execute(self.db, {'action': action, **kw})

    def artifact(self, jid):
        with store.transaction(self.db) as db:
            found = store.row(db, 'SELECT * FROM artifacts WHERE job=?', (jid,))
            return found['status'] if found else None

    def proof(self, jid):
        with store.transaction(self.db) as db:
            return lineage.child_proof(db, jid)

    def job(self, jid, parents=(), deadline=100, tenant='t'):
        return self.act('job.submit', id=jid, tenant=tenant, deadline=deadline, duration=10, cpu=1, memory=1, created=0, parents=list(parents))

    def slot(self, slot='s', at=1):
        return self.act('slot.register', id=slot, node='n', cpu=1, memory=1, at=at)

    def lease_start(self, jid, at, expires, generation=1):
        self.act('slot.lease', id=jid, slot='s', at=at, expires=expires)
        return self.act('run.start', id=jid, generation=generation, at=at)

    def deliver(self, jid, content, at, generation=1):
        return self.act('run.settle', id=jid, generation=generation, at=at, digest=digest(content), content=content)

    def two_level_delivery(self):
        self.job('A')
        self.job('B', parents=['A'])
        self.slot()
        self.lease_start('A', at=10, expires=25)
        self.deliver('A', 'a', at=15)
        self.lease_start('B', at=25, expires=40)
        self.deliver('B', 'b', at=30)


class CoreScenarioTests(LineageBase):
    """The exact scenario from the requirement, both legal dispositions."""

    def build(self, shared_c=False):
        self.job('A')
        self.job('B', parents=['A'])
        if shared_c:
            self.job('C', parents=['A'], deadline=120)
        self.slot()
        # A leases the slot at minute 10 and delivers content a at minute 15.
        self.lease_start('A', at=10, expires=25)
        self.assertEqual(self.deliver('A', 'a', at=15), 'delivered')
        # B leases at minute 25 and delivers content b at minute 30.
        self.lease_start('B', at=25, expires=40)
        self.assertEqual(self.deliver('B', 'b', at=30), 'delivered')
        if shared_c:
            self.lease_start('C', at=40, expires=55)
            self.assertEqual(self.deliver('C', 'c', at=45), 'delivered')
        self.act('run.retention', id='A', expires=40, hold=False, reason='standard', at=40)
        self.act('run.retention', id='B', expires=100, hold=True, reason='legal', at=30)
        if shared_c:
            self.act('run.retention', id='C', expires=120, hold=True, reason='legal', at=45)

    def test_keep_parent_online_is_a_legal_disposition(self):
        self.build()
        self.assertEqual(self.act('run.purge', at=45), 'deferred')
        self.assertEqual(self.artifact('A'), 'delivered')
        self.assertEqual(self.act('job.audit', id='B'), 'delivered')
        self.assertEqual(self.artifact('B'), 'delivered')

    def test_sealed_proof_permits_real_release_of_parent_bytes(self):
        self.build()
        self.assertEqual(self.act('run.seal', id='B', at=35), 'sealed')
        self.assertTrue(self.proof('B')['complete'])
        self.assertEqual(self.act('run.purge', at=45), 'purged')
        self.assertEqual(self.artifact('A'), 'purged')
        # B is still auditable and still holds its own delivered bytes.
        self.assertEqual(self.act('job.audit', id='B'), 'delivered')
        self.assertEqual(self.act('job.verify_lineage', id='B'), 'lineage_verified')
        self.assertEqual(self.artifact('B'), 'delivered')

    def test_unsealed_child_blocks_parent_purge_and_purge_reports_deferred(self):
        self.build()
        self.assertEqual(self.act('run.purge', at=45), 'deferred')
        with store.transaction(self.db) as db:
            deferred = store.rows(db, "SELECT decision,evidence FROM events WHERE kind='purge' AND decision='deferred'")
        self.assertTrue(deferred)
        self.assertEqual(self.artifact('A'), 'delivered')

    def test_shared_parent_needs_every_in_force_child_proven(self):
        self.build(shared_c=True)
        self.assertEqual(self.act('run.seal', id='B', at=35), 'sealed')
        # C is still protected through minute 120 and has no seal yet.
        self.assertEqual(self.act('run.purge', at=45), 'deferred')
        self.assertEqual(self.artifact('A'), 'delivered')
        self.assertEqual(self.act('run.seal', id='C', at=50), 'sealed')
        self.assertEqual(self.act('run.purge', at=45), 'purged')
        self.assertEqual(self.artifact('A'), 'purged')
        self.assertEqual(self.act('job.audit', id='B'), 'delivered')
        self.assertEqual(self.act('job.audit', id='C'), 'delivered')
        self.assertEqual(self.act('job.verify_lineage', id='C'), 'lineage_verified')


class ProofContentTests(LineageBase):
    def test_seal_pins_digest_attempt_generation_delivery_and_closure(self):
        self.two_level_delivery()
        self.assertEqual(self.act('run.seal', id='B', at=35), 'sealed')
        with store.transaction(self.db) as db:
            seal = store.row(db, 'SELECT * FROM lineage_seals WHERE child=? AND parent=?', ('B', 'A'))
            payload = json.loads(seal['payload'])
        self.assertEqual(payload['child_delivery']['digest'], digest('b'))
        self.assertEqual(payload['child_delivery']['generation'], 1)
        self.assertEqual(payload['child_delivery']['at'], 30)
        node = payload['closure'][0]
        self.assertEqual(node['job'], 'A')
        self.assertEqual(node['digest'], digest('a'))
        self.assertEqual(node['generation'], 1)
        self.assertEqual(node['delivered_at'], 15)
        self.assertEqual(node['attempt_status'], 'success')
        self.assertTrue(payload['authorize_parent_purge'])

    def test_seal_is_signed_and_unsigned_or_mutated_payloads_are_rejected(self):
        self.two_level_delivery()
        self.act('run.seal', id='B', at=35)
        with store.transaction(self.db) as db:
            seal = store.row(db, 'SELECT * FROM lineage_seals WHERE child=? AND parent=?', ('B', 'A'))
            _, errors = lineage.verify_seal(db, seal)
            self.assertEqual(errors, [])
            # Flip a byte in the persisted payload without re-signing.
            tampered = seal['payload'].replace('"generation":1', '"generation":9', 1)
            _, errors = lineage.verify_seal(db, {**seal, 'payload': tampered})
            self.assertIn('signature_mismatch', errors)

    def test_missing_attempt_generation_cannot_pass_as_evidence(self):
        self.two_level_delivery()
        with store.transaction(self.db) as db:
            artifact = store.row(db, 'SELECT * FROM artifacts WHERE job=?', ('B',))
            attempt = store.row(db, 'SELECT * FROM attempts WHERE job=? AND status=?', ('B', 'success'))
            forged = {
                'schema': lineage.SCHEMA_VERSION,
                'child': 'B',
                'parent': 'A',
                'child_version': 1,
                'child_delivery': {'digest': artifact['digest'], 'generation': 1, 'at': artifact['delivered']},
                # A deliberately omits the successful attempt generation.
                'child_attempt': {'job': 'B', 'status': 'success', 'started': attempt['started'], 'ended': attempt['ended'], 'digest': artifact['digest']},
                'declared_parents': ['A'],
                'closure': [{'job': 'A', 'version': 1, 'digest': digest('a'), 'attempt_status': 'success', 'delivered_at': 15}],
                'sealed_at': 35,
                'authorize_parent_purge': True,
            }
            text = store.canonical_json(forged)
            db.execute(
                'INSERT INTO lineage_seals(child,parent,sealed_at,payload,signature) VALUES(?,?,?,?,?)',
                ('B', 'A', 35, text, store.signature(db, text)),
            )
            # The forged row has a valid keyed signature for its text, but its
            # content is incomplete and must not count as proof.
            verified = lineage.verified_seals(db, 'B')
            _, errors = lineage.verify_seal(db, store.row(db, 'SELECT * FROM lineage_seals WHERE child=?', ('B',)))
        self.assertEqual(len(verified), 0)
        self.assertIn('closure_attempt_incomplete:A', errors)
        self.assertIn('attempt_generation_mismatch', errors)
        self.assertIn('seal_missing:A', self.proof('B')['errors'])
        self.act('run.retention', id='B', expires=100, hold=True, reason='legal', at=30)
        self.act('run.retention', id='A', expires=40, hold=False, reason='standard', at=40)
        self.assertEqual(self.act('run.purge', at=45), 'deferred')
        self.assertEqual(self.artifact('A'), 'delivered')

    def test_seal_rejects_incomplete_lineage_when_ancestor_attempt_is_missing(self):
        self.two_level_delivery()
        with store.transaction(self.db) as db:
            db.execute('UPDATE attempts SET status=? WHERE job=? AND generation=?', ('failed', 'A', 1))
        self.assertEqual(self.act('run.seal', id='B', at=35), 'incomplete_lineage')
        self.assertFalse(self.proof('B')['complete'])


class RetryAndStalenessTests(LineageBase):
    def test_seal_pins_the_successful_retry_generation_not_a_failed_one(self):
        self.job('A')
        self.job('B', parents=['A'])
        self.slot()
        self.lease_start('A', at=2, expires=20)
        self.act('run.start', id='A', generation=1, at=3)
        self.assertEqual(self.act('run.settle', id='A', generation=1, at=10, success=False, digest=None), 'retryable')
        self.act('slot.lease', id='A', slot='s', at=11, expires=30)
        self.act('run.start', id='A', generation=2, at=12)
        self.assertEqual(self.deliver('A', 'a', at=15, generation=2), 'delivered')
        self.lease_start('B', at=16, expires=30)
        self.assertEqual(self.deliver('B', 'b', at=20), 'delivered')
        self.assertEqual(self.act('run.seal', id='B', at=35), 'sealed')
        with store.transaction(self.db) as db:
            payload = json.loads(store.row(db, 'SELECT payload FROM lineage_seals WHERE child=? AND parent=?', ('B', 'A'))['payload'])
        self.assertEqual(payload['closure'][0]['generation'], 2)
        self.assertEqual(payload['closure'][0]['digest'], digest('a'))

    def test_stale_generation_receipt_cannot_overwrite_a_delivery(self):
        self.job('A')
        self.slot()
        self.lease_start('A', at=10, expires=25)
        self.assertEqual(self.deliver('A', 'a', at=15), 'delivered')
        # Re-settling an old generation after delivery must not rewrite evidence.
        self.assertEqual(self.act('run.settle', id='A', generation=1, at=16, digest=digest('other'), content='other'), 'stale_attempt')
        with store.transaction(self.db) as db:
            delivery = store.row(db, 'SELECT * FROM deliveries WHERE job=?', ('A',))
        self.assertEqual(delivery['digest'], digest('a'))
        self.assertEqual(delivery['at'], 15)

    def test_repeated_or_concurrent_delivery_does_not_rewrite_first_event(self):
        self.job('A')
        self.slot()
        self.lease_start('A', at=10, expires=25)
        self.assertEqual(self.deliver('A', 'a', at=15), 'delivered')
        with store.transaction(self.db) as db:
            first = store.row(db, 'SELECT * FROM deliveries WHERE job=?', ('A',))
        # A conference re-runs settle: it is rejected as a stale attempt and the
        # pinned delivery event is unchanged.
        self.assertIn(self.act('run.settle', id='A', generation=1, at=16, digest=digest('a'), content='a'), ('stale_attempt', 'idempotent'))
        with store.transaction(self.db) as db:
            after = store.row(db, 'SELECT * FROM deliveries WHERE job=?', ('A',))
            self.assertEqual(after['snapshot'], first['snapshot'])
            self.assertEqual(after['at'], 15)

    def test_parent_cancellation_after_child_delivery_does_not_erase_it(self):
        self.two_level_delivery()
        self.act('run.retention', id='B', expires=100, hold=True, reason='legal', at=30)
        self.assertEqual(self.act('run.seal', id='B', at=35), 'sealed')
        self.assertEqual(self.act('job.cancel', id='B', at=36, reason='legal-hold'), 'already_delivered')
        with store.transaction(self.db) as db:
            pending = store.row(db, "SELECT * FROM cancellations WHERE job=?", ('B',))
        self.assertIsNone(pending)
        self.assertEqual(self.act('job.audit', id='B'), 'delivered')
        self.assertEqual(self.act('run.purge', at=45), 'purged')
        self.assertEqual(self.act('job.audit', id='B'), 'delivered')


class RecoveryTests(LineageBase):
    def test_purge_of_own_bytes_keeps_job_auditable_from_pinned_delivery(self):
        self.job('A')
        self.slot()
        self.lease_start('A', at=10, expires=25)
        self.deliver('A', 'a', at=15)
        self.act('run.retention', id='A', expires=30, hold=False, reason='standard', at=30)
        self.assertEqual(self.act('run.purge', at=45), 'purged')
        self.assertEqual(self.artifact('A'), 'purged')
        self.assertEqual(self.act('job.audit', id='A'), 'delivered')

    def test_reopening_a_database_preserves_seals_and_decision_chain(self):
        self.two_level_delivery()
        self.act('run.seal', id='B', at=35)
        with store.transaction(self.db) as db:
            count = store.row(db, 'SELECT count(*) AS n FROM lineage_seals')['n']
            head = store.row(db, 'SELECT digest FROM decisions ORDER BY seq DESC LIMIT 1')['digest']
        self.assertEqual(count, 1)
        # Reopen: a new connection must verify the same signed payload.
        with store.transaction(self.db) as db:
            self.assertEqual(store.verify_decisions(db)['valid'], True)
            self.assertEqual(store.verify_decisions(db)['head'], head)
            self.assertEqual(len(lineage.verified_seals(db, 'B')), 1)

    def test_decision_chain_detects_erasure_or_reordering(self):
        self.two_level_delivery()
        self.act('run.seal', id='B', at=35)
        with store.transaction(self.db) as db:
            self.assertTrue(store.verify_decisions(db)['valid'])
            db.execute("DELETE FROM decisions WHERE seq=2")
        with store.transaction(self.db) as db:
            result = store.verify_decisions(db)
        self.assertFalse(result['valid'])
        self.assertIn('broken_prev', {e['reason'] for e in result['errors']})

    def test_old_database_is_upgraded_in_place_without_losing_rows(self):
        # A database produced before lineage tables existed (schema v0).
        target = str(Path(self.temp.name) / 'legacy.sqlite')
        legacy = sqlite3.connect(target)
        legacy.executescript(store.BASE_SCHEMA)
        legacy.execute("INSERT INTO jobs VALUES('old','t',100,10,1,1,'delivered',1,0)")
        legacy.execute("INSERT INTO attempts VALUES('old',1,'success',3,20,?)", (digest('a'),))
        legacy.execute("INSERT INTO artifacts VALUES('old',?,20,'delivered')", (digest('a'),))
        legacy.execute("INSERT INTO retentions VALUES('old',40,0,'standard')")
        legacy.commit()
        legacy.close()
        # Reopen the legacy path with the new build: additive tables appear and
        # every original row survives; the delivery is still auditable.
        old_act = lambda action, **kw: execute(target, {'action': action, **kw})['decision']
        self.assertEqual(old_act('job.audit', id='old'), 'delivered')
        self.assertEqual(old_act('run.seal', id='old', at=25), 'sealed_trivial')
        self.assertEqual(old_act('run.purge', at=45), 'purged')
        self.assertEqual(old_act('job.audit', id='old'), 'delivered')
        with store.transaction(target) as db:
            self.assertEqual(store.row(db, 'SELECT count(*) AS n FROM jobs')['n'], 1)
            self.assertTrue(store.row(db, 'SELECT * FROM deliveries WHERE job=?', ('old',)))


class LegacyFixtureTests(LineageBase):
    """Keep the shipped fixtures and the original delivery paths working."""

    def test_demo_fixture_still_holds_and_audits(self):
        for request in json.loads(Path('fixtures/demo.json').read_text(encoding='utf-8')):
            execute(self.db, request)
        self.assertEqual(self.act('job.audit', id='build-a'), 'delivered')

    def test_failed_attempt_retries_then_delivers(self):
        self.job('A')
        self.slot()
        self.lease_start('A', at=2, expires=20)
        self.act('run.start', id='A', generation=1, at=3)
        self.assertEqual(self.act('run.settle', id='A', generation=1, at=10, success=False, digest=None), 'retryable')
        self.act('slot.lease', id='A', slot='s', at=11, expires=30)
        self.act('run.start', id='A', generation=2, at=12)
        self.assertEqual(self.deliver('A', 'a', at=15, generation=2), 'delivered')
        self.assertEqual(self.act('job.audit', id='A'), 'delivered')


if __name__ == '__main__':
    unittest.main()
