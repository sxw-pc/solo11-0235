import tempfile
import unittest
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
    def test_attempt_delivery_and_retention(self):
        self.job();self.slot();self.lease()
        self.assertEqual(self.run_action('run.start',id='j',generation=1,at=3),'running')
        self.assertEqual(self.run_action('run.settle',id='j',generation=1,at=20,digest='hash'),'delivered')
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

if __name__=='__main__':unittest.main()
