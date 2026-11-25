import os
import tempfile
import unittest

from cloudctl import store, jobs, slots
from cloudctl.secret_envelope import register, mount


class SecretEnvelopeTest(unittest.TestCase):
    def test_mount_follows_live_lease_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = os.path.join(folder, 'state.sqlite')
            store.init(db_path)
            with store.transaction(db_path) as db:
                jobs.submit(db, {'id':'j','tenant':'t','created':0,'deadline':100,'duration':10,'cpu':1})
                slots.register(db, {'id':'s','cpu':2,'node':'n','at':1})
                slots.lease(db, 'j','s',2,90)
                self.assertEqual(register(db,'j','k',1,'sha256:abc')['decision'],'registered')
                self.assertEqual(mount(db,'j',1,3)['decision'],'issued')
                self.assertEqual(mount(db,'j',0,3)['decision'],'unavailable')
