import sqlite3,unittest
from cloudctl.usage_capture import import_page
from cloudctl.invoice import issue
from cloudctl.prepaid import open_account,debit
class UsageSeedTests(unittest.TestCase):
 def test_meter_to_invoice_to_written_balance(self):
  db=sqlite3.connect(':memory:')
  rows=[{'node':'n1','tenant':'t1','job':'j1','ticks':20}]
  open_account(db,'t1',100)
  self.assertEqual(import_page(db,'p1',rows)['status'],'captured')
  self.assertEqual(issue(db,'i1','t1')['ticks'],20)
  self.assertEqual(debit(db,'i1')['balance'],80)
  self.assertEqual(import_page(db,'p1',rows)['status'],'replayed')
  with self.assertRaises(ValueError):import_page(db,'p1',[{**rows[0],'ticks':21}])
