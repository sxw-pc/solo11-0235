import tempfile
import unittest
import hashlib
from pathlib import Path
from cloudctl import store
from cloudctl.__main__ import execute

print('MODULE_PATH',Path(store.__file__).resolve())

class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=str(Path(self.temp.name)/'test.sqlite')
        store.init(self.db)
    def tearDown(self):self.temp.cleanup()
    def run_action(self,action,**kw):return execute(self.db,{'action':action,**kw})['decision']
    def job(self,jid='j',**kw):
        r={'id':jid,'tenant':'tenant','deadline':100,'duration':10,'cpu':2,'created':0}
        r.update(kw);return self.run_action('job.submit',**r)
    def slot(self):return self.run_action('slot.register',id='s',node='n',cpu=4,at=1)
    def lease(self,jid='j',**kw):return self.run_action('slot.lease',id=jid,slot='s',at=2,expires=50,**kw)
    def test_job_contract_and_revision(self):
        self.assertEqual(self.job(parents=['missing']),'rejected_dependency')
        self.assertEqual(self.job(),'accepted')
        self.assertEqual(self.job(),'idempotent')
        self.assertEqual(self.run_action('job.revise',id='j',deadline=110,expected=9),'stale_version')
        self.assertEqual(self.run_action('job.revise',id='j',deadline=110,expected=1),'revised')
    def test_lease_preemption_and_node_loss(self):
        self.job();self.slot();self.assertEqual(self.lease(),'leased')
        self.job('urgent')
        self.assertEqual(self.lease('urgent'),'occupied')
        self.assertEqual(self.lease('urgent',preempt=True),'leased')
        self.assertEqual(self.run_action('slot.heartbeat',slot='s',at=4,healthy=False),'isolated')
        self.assertEqual(self.run_action('run.start',id='urgent',generation=1,at=5),'not_leased')
    def test_preempted_work_receives_repayment_slot(self):
        self.job();self.slot();self.lease();self.job('urgent')
        self.lease('urgent',preempt=True)
        self.run_action('slot.register',id='spare',node='n2',cpu=4,at=3)
        self.assertEqual(self.run_action('slot.repay',id='j',slot='spare',at=4),'repaid')
        self.assertEqual(self.run_action('run.start',id='j',generation=2,at=5),'running')
    def test_resource_vector_and_descendant_deadlines(self):
        self.job(memory=8);self.slot()
        self.assertEqual(self.lease(),'capacity_or_deadline')
        self.run_action('slot.register',id='large',node='n2',cpu=4,memory=16,at=2)
        self.assertEqual(self.run_action('slot.lease',id='j',slot='large',at=3,expires=50),'leased')
        self.job('child',deadline=200,parents=['j'])
        self.job('grandchild',deadline=150,parents=['child'])
        self.assertEqual(self.run_action('job.revise',id='j',deadline=170,expected=1),'blocked_successors')
    def test_invalid_resources_and_windows_do_not_create_leases(self):
        self.assertEqual(self.job(cpu=-1),'invalid_resource')
        self.assertEqual(self.run_action('slot.register',id='invalid',node='n',cpu=4,memory=-1,at=1),'invalid_resource')
        self.job();self.slot()
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=20,expires=10),'invalid_window')
    def test_attempt_delivery_and_retention(self):
        self.job();self.slot();self.lease()
        self.assertEqual(self.run_action('run.start',id='j',generation=1,at=3),'running')
        digest='sha256:'+hashlib.sha256(b'payload').hexdigest()
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=20,digest=digest,content='payload'),'delivered')
        self.assertEqual(self.run_action('run.retention',id='j',expires=30,hold=True,reason='audit'),'held')
        self.assertEqual(self.run_action('run.purge',at=40),'purged')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db,'SELECT status FROM artifacts WHERE job=?',('j',))['status'],'delivered')
    def test_failure_retry_and_cancellation(self):
        self.job();self.slot();self.lease()
        self.run_action('run.start',id='j',generation=1,at=3)
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=20,success=False),'retryable')
        self.assertEqual(self.run_action('job.cancel',id='j',at=21,reason='operator'),'cancelled')
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=22,digest='late'),'stale_attempt')
    def test_digest_mismatch_preserves_running_attempt(self):
        self.job();self.slot();self.lease();self.run_action('run.start',id='j',generation=1,at=3)
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=20,digest='bad',content='payload'),'digest_mismatch')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db,'SELECT status FROM attempts WHERE job=?',('j',))['status'],'running')
    def test_expired_lease_cannot_start_or_deliver(self):
        digest='sha256:'+hashlib.sha256(b'payload').hexdigest()
        self.job();self.slot()
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=2,expires=5),'leased')
        self.assertEqual(self.run_action('run.start',id='j',generation=1,at=6),'lease_expired')
        with store.transaction(self.db) as db:self.assertIsNone(store.row(db,'SELECT * FROM attempts WHERE job=?',('j',)))
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=7,expires=50),'leased')
        self.assertEqual(self.run_action('run.start',id='j',generation=2,at=8),'running')
        self.assertEqual(self.run_action('run.settle',id='j',generation=2,at=51,digest=digest,content='payload'),'lease_expired')
        with store.transaction(self.db) as db:
            self.assertIsNone(store.row(db,'SELECT * FROM artifacts WHERE job=?',('j',)))
            self.assertEqual(store.row(db,'SELECT status FROM attempts WHERE job=? AND generation=?',('j',2))['status'],'expired')
    def test_stale_slot_heartbeat_cannot_start_or_deliver(self):
        self.job(deadline=200);self.slot()
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=2,expires=150),'leased')
        self.assertEqual(self.run_action('run.start',id='j',generation=1,at=63),'slot_unavailable')
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=64,expires=150),'slot_unavailable')
        self.assertEqual(self.run_action('slot.heartbeat',slot='s',at=65,healthy=True),'healthy')
        self.assertEqual(self.run_action('slot.lease',id='j',slot='s',at=66,expires=150),'leased')
        self.assertEqual(self.run_action('run.start',id='j',generation=2,at=67),'running')
        self.assertEqual(self.run_action('run.settle',id='j',generation=2,at=127,digest='bad',content='payload'),'slot_unavailable')
    def test_cancel_blocks_successors_and_releases_slot(self):
        self.job();self.job('child',parents=['j']);self.job('grandchild',parents=['child']);self.slot();self.lease()
        self.assertEqual(self.run_action('job.cancel',id='j',at=3,reason='operator'),'cancelled')
        with store.transaction(self.db) as db:
            self.assertEqual(store.row(db,'SELECT status FROM jobs WHERE id=?',('child',))['status'],'blocked_parent')
            self.assertEqual(store.row(db,'SELECT status FROM jobs WHERE id=?',('grandchild',))['status'],'blocked_parent')
            self.assertEqual(store.row(db,'SELECT status FROM leases WHERE job=?',('j',))['status'],'released')
    def test_parent_artifact_changes_child_delivery_decision(self):
        digest='sha256:'+hashlib.sha256(b'payload').hexdigest()
        self.job();self.job('child',parents=['j']);self.slot();self.lease('child')
        self.run_action('run.start',id='child',generation=1,at=3)
        self.assertEqual(self.run_action('run.settle',id='child',generation=1,at=20,digest=digest,content='payload'),'delivery_blocked')
        self.assertEqual(self.run_action('run.finalize',id='child',generation=1,at=21),'waiting_parents')
        self.run_action('slot.register',id='parent-slot',node='n2',cpu=4,at=2)
        self.run_action('slot.lease',id='j',slot='parent-slot',at=3,expires=50)
        self.run_action('run.start',id='j',generation=1,at=4)
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=15,digest=digest,content='payload'),'delivered')
        self.assertEqual(self.run_action('run.finalize',id='child',generation=1,at=22),'delivered')
        self.assertEqual(self.run_action('job.audit',id='child'),'delivered')

if __name__=='__main__':unittest.main()
