import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from cloudctl import store, jobs, slots, runs, usage_capture, invoice as invoice_module, prepaid
from cloudctl import settlement
from cloudctl.settlement import MockBank, SettlementError

DIGEST = 'sha256:' + hashlib.sha256(b'payload').hexdigest()


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'state.sqlite')
        store.init(self.db)
        self.bank = MockBank(str(Path(self.tmp.name) / 'bank.sqlite'))

    def tearDown(self):
        self.bank.close()
        self.tmp.cleanup()

    def tx(self):
        return store.transaction(self.db)

    def run_job(self, jid, tenant='t1', succeed=True):
        with self.tx() as db:
            jobs.submit(db, {'id': jid, 'tenant': tenant, 'deadline': 1000, 'duration': 10, 'cpu': 1, 'created': 0})
            slots.register(db, {'id': 'slot-' + jid, 'node': 'node-' + jid, 'cpu': 4, 'at': 1})
            slots.lease(db, jid, 'slot-' + jid, 2, 500)
            runs.start(db, jid, 1, 3)
            if succeed:
                runs.settle(db, jid, 1, 20, DIGEST, success=True, content='payload')
            else:
                runs.settle(db, jid, 1, 20, None, success=False)

    def retry_job(self, jid, generation=2):
        with self.tx() as db:
            slots.lease(db, jid, 'slot-' + jid, 30, 500)
            runs.start(db, jid, generation, 40)
            runs.settle(db, jid, generation, 50, DIGEST, success=True, content='payload')

    def meter(self, page, rows):
        with self.tx() as db:
            return settlement.ingest_meter_page(db, page, rows)

    def setup_world(self):
        """去重后 60 单位(j1=40,j2=20);失败作业 j3=10 不计费。
        t1 预付 30;组织 O 共享 80,担保 g1 授权 t1 至多 60。"""
        self.run_job('j1')
        self.run_job('j2')
        self.run_job('j3', succeed=False)
        with self.tx() as db:
            settlement.ingest_meter_page(db, 'p1', [
                {'node': 'n1', 'tenant': 't1', 'job': 'j1', 'generation': 1, 'counter_start': 0, 'counter_end': 40},
                {'node': 'n2', 'tenant': 't1', 'job': 'j2', 'generation': 1, 'counter_start': 0, 'counter_end': 20},
                {'node': 'n3', 'tenant': 't1', 'job': 'j3', 'generation': 1, 'counter_start': 0, 'counter_end': 10},
            ])
            settlement.open_prepaid(db, 't1', 30)
            settlement.open_org(db, 'O', 80)
            settlement.grant_guarantee(db, 'g1', 'O', 't1', 60, expires=1000, at=1)
            inv = settlement.issue_invoice(db, 'inv1', 't1', at=2)
        self.assertEqual(inv['ticks'], 60)

    def setup_t2(self, ticks=70, grant=70):
        self.run_job('j4', tenant='t2')
        with self.tx() as db:
            settlement.ingest_meter_page(db, 'p-t2', [
                {'node': 'n4', 'tenant': 't2', 'job': 'j4', 'generation': 1, 'counter_start': 0, 'counter_end': ticks}])
            settlement.grant_guarantee(db, 'g2', 'O', 't2', grant, expires=1000, at=3)
            return settlement.issue_invoice(db, 'inv2', 't2', at=4)

    # ---------------------------------------------------------- metering

    def test_counter_dedup_across_pages_migration_and_retry(self):
        self.run_job('j1')
        row = {'node': 'n1', 'tenant': 't1', 'job': 'j1', 'generation': 1, 'counter_start': 0, 'counter_end': 40}
        self.assertEqual(self.meter('p1', [row]), {'status': 'captured', 'ingested': 1, 'duplicates': 0})
        # 同一页重扫:不再入库
        self.assertEqual(self.meter('p1', [row]), {'status': 'replayed', 'ingested': 0, 'duplicates': 1})
        # 迁移后另一张扫描页携带同一(机器,代次,区间):仍是同一段计算
        self.assertEqual(self.meter('p2', [row])['duplicates'], 1)
        # 失败重试的新代次是不同计算段
        self.assertEqual(self.meter('p3', [dict(row, generation=2, counter_end=15)])['ingested'], 1)
        # 同机同代次的下一段计数区间也是新计算
        self.assertEqual(self.meter('p4', [dict(row, counter_start=40, counter_end=55)])['ingested'], 1)
        # 同一计数键却报不同租户:冲突,拒绝入库
        with self.assertRaises(SettlementError) as ctx:
            self.meter('p5', [dict(row, tenant='tX')])
        self.assertEqual(str(ctx.exception), 'counter_conflict')
        # 计量值必须与计数区间一致
        with self.assertRaises(SettlementError) as ctx:
            self.meter('p6', [dict(row, counter_start=100, counter_end=110, ticks=99)])
        self.assertEqual(str(ctx.exception), 'invalid_counter_row')
        with self.tx() as db:
            total = db.execute('SELECT COALESCE(SUM(ticks),0) FROM usage_dedup').fetchone()[0]
        self.assertEqual(total, 40 + 15 + 15)

    def test_invoice_excludes_failed_jobs_and_never_bills_twice(self):
        self.setup_world()
        with self.tx() as db:
            st = settlement.invoice_status(db, 'inv1')
            self.assertEqual((st['ticks'], st['status'], st['outstanding']), (60, 'open', 60))
            self.assertTrue(settlement.verify_invoice(db, 'inv1')['consistent'])
            # 幂等重开
            again = settlement.issue_invoice(db, 'inv1', 't1', at=3)
            self.assertEqual((again['status'], again['ticks']), ('replayed', 60))
            # 失败作业 j3 的 10 单位留在未开票池
            unbilled = db.execute('SELECT COALESCE(SUM(ticks),0) FROM usage_dedup WHERE invoice_id IS NULL').fetchone()[0]
            self.assertEqual(unbilled, 10)
        # j3 重试成功后,存量计量行进入下一张发票;已开票的 60 不会重复计费
        self.retry_job('j3')
        with self.tx() as db:
            self.assertEqual(settlement.issue_invoice(db, 'inv3', 't1', at=4)['ticks'], 10)
            with self.assertRaises(SettlementError) as ctx:
                settlement.issue_invoice(db, 'inv4', 't1', at=5)
            self.assertEqual(str(ctx.exception), 'nothing_billable')
            self.assertEqual(settlement.check_invariants(db), [])

    # ---------------------------------------------------------- approach 1: freeze bank, org transfer

    def test_approach1_freeze_untouched_bank_request_then_org_transfer(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=3)
        # 网关超时,银行端尚未发起扣款
        self.assertEqual(settlement.submit_bank(self.db, self.bank, 'x', at=4, mode='timeout_before')['status'], 'unknown')
        # 经银行事务号核实未扣款 → 冻结请求并释放银行预留
        self.assertEqual(settlement.freeze_bank(self.db, self.bank, 'x', at=5)['status'], 'frozen')
        self.assertEqual(self.bank.inquire(request_id='x')['status'], 'voided')
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'org', 60, 'g1', at=6)
            self.assertTrue(settlement.capture(db, 'inv1:org:g1', at=7)['settled'])
            st = settlement.invoice_status(db, 'inv1')
            self.assertEqual((st['status'], st['captured']), ('settled', 60))
            # t1 的 30 仍可用于下一单;担保 60 已用尽
            credit = settlement.available_credit(db, 't1', at=8)
            self.assertEqual((credit['prepaid'], credit['org']), (30, 0))
            self.assertEqual(tuple(db.execute('SELECT balance, held FROM org_accounts WHERE org=?', ('O',)).fetchone()), (20, 0))
            self.assertTrue(settlement.verify_invoice(db, 'inv1')['consistent'])
            self.assertEqual(settlement.check_invariants(db), [])
        # 没有任何真实银行流水
        self.assertEqual(self.bank.ledger(), [])
        # 组织只剩 20:t2 申请 70 共享信用被拒
        self.setup_t2()
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv2', 'org', 70, 'g2', at=8)
        self.assertEqual(str(ctx.exception), 'insufficient_org_credit')

    # ---------------------------------------------------------- approach 2: prepaid + idempotent bank

    def test_approach2_prepaid_then_bank_with_lost_receipt(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=3)
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=3)
            settlement.capture(db, 'inv1:prepaid:t1', at=4)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'open')
        # 网关超时,回执丢失,银行端实际已扣款
        self.assertEqual(settlement.submit_bank(self.db, self.bank, 'x', at=5, mode='timeout_after')['status'], 'unknown')
        with self.tx() as db:
            # 未核实前:发票不得标为已付,银行预留不得捕获
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'open')
            with self.assertRaises(SettlementError) as ctx:
                settlement.capture(db, 'inv1:bank:x', at=6)
            self.assertEqual(str(ctx.exception), 'bank_unconfirmed')
        # 按银行事务号核实:远端确已收款 → 确认后才捕获入账
        self.assertEqual(settlement.reconcile_bank(self.db, self.bank, 'x', at=7)['status'], 'confirmed')
        with self.tx() as db:
            self.assertTrue(settlement.capture(db, 'inv1:bank:x', at=8)['settled'])
            st = settlement.invoice_status(db, 'inv1')
            self.assertEqual((st['status'], st['captured']), ('settled', 60))
            self.assertEqual(tuple(db.execute('SELECT balance, held FROM prepaid_accounts WHERE tenant=?', ('t1',)).fetchone()), (0, 0))
            # 组织 80 未动,仍可供其他子租户
            credit = settlement.available_credit(db, 't1', at=9)
            self.assertEqual((credit['prepaid'], credit['org']), (0, 60))
            self.assertTrue(settlement.verify_invoice(db, 'inv1')['consistent'])
            self.assertEqual(settlement.check_invariants(db), [])
        # 真实银行流水恰好一笔 30
        self.assertEqual(self.bank.net('t1'), 30)
        self.assertEqual(len(self.bank.ledger()), 1)
        # t2 的 70 共享信用可得
        self.setup_t2()
        with self.tx() as db:
            self.assertEqual(settlement.reserve(db, 'inv2', 'org', 70, 'g2', at=9)['status'], 'held')
            self.assertEqual(settlement.available_credit(db, 't2', at=9)['org'], 0)
            self.assertEqual(settlement.check_invariants(db), [])

    def test_blind_retry_cannot_double_charge(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=3)
        settlement.submit_bank(self.db, self.bank, 'x', at=4, mode='timeout_after')
        # 本地防线:未知状态下禁止盲重试
        with self.assertRaises(SettlementError) as ctx:
            settlement.submit_bank(self.db, self.bank, 'x', at=5)
        self.assertEqual(str(ctx.exception), 'bank_not_submittable')
        # 银行端幂等:同一请求号只返回原回执,不会再次扣款
        self.assertTrue(self.bank.debit('x', 't1', 30, at=5)['replayed'])
        self.assertEqual(self.bank.net('t1'), 30)
        self.assertEqual(len(self.bank.ledger()), 1)
        # 正确路径:核实 → 确认 → 捕获,发票只清偿一次
        settlement.reconcile_bank(self.db, self.bank, 'x', at=6)
        with self.tx() as db:
            settlement.capture(db, 'inv1:bank:x', at=7)
            settlement.reserve(db, 'inv1', 'org', 30, 'g1', at=8)
            self.assertTrue(settlement.capture(db, 'inv1:org:g1', at=9)['settled'])
            self.assertEqual(settlement.check_invariants(db), [])
        self.assertEqual(len(self.bank.ledger()), 1)

    # ---------------------------------------------------------- crash recovery

    def test_crash_recovery_commit_boundaries(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=3)
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=3)
            settlement.capture(db, 'inv1:prepaid:t1', at=4)
        # 断电前最后一幕:银行请求已提交,网关超时,银行端尚未扣款
        self.assertEqual(settlement.submit_bank(self.db, self.bank, 'x', at=5, mode='timeout_before')['status'], 'unknown')
        # 恢复后(全新连接):预付捕获是持久的,发票未付,请求处于可恢复的未知态
        with self.tx() as db:
            st = settlement.invoice_status(db, 'inv1')
            self.assertEqual((st['status'], st['captured']), ('open', 30))
            self.assertEqual(store.row(db, 'SELECT status FROM bank_requests WHERE id=?', ('x',))['status'], 'in_flight')
            self.assertEqual(settlement.available_credit(db, 't1', at=6)['prepaid'], 0)
            self.assertEqual(settlement.check_invariants(db), [])
        # 核实:银行未扣款 → 用同一请求号幂等补发
        reconciled = settlement.reconcile_bank(self.db, self.bank, 'x', at=7)
        self.assertEqual(reconciled['status'], 'confirmed')
        with self.tx() as db:
            self.assertTrue(settlement.capture(db, 'inv1:bank:x', at=8)['settled'])
        # 恢复动作本身可重入,不会重复入账
        self.assertEqual(settlement.reconcile_bank(self.db, self.bank, 'x', at=9)['bank_txn'], reconciled['bank_txn'])
        with self.tx() as db:
            self.assertEqual(settlement.capture(db, 'inv1:bank:x', at=10)['status'], 'replayed')
            kinds = [r[0] for r in db.execute('SELECT kind FROM ledger WHERE invoice_id=?', ('inv1',))]
            self.assertEqual(sorted(kinds), ['bank_capture', 'prepaid_debit'])
            self.assertEqual(settlement.check_invariants(db), [])
        self.assertEqual(len(self.bank.ledger()), 1)

    # ---------------------------------------------------------- shared credit contention

    def test_shared_credit_contention_release_and_rehold(self):
        self.setup_world()
        self.setup_t2()
        # t1 先占 60 组织信用(尚未捕获)
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'org', 60, 'g1', at=5)
        # t2 同时申请 70 → 共享余额不足
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv2', 'org', 70, 'g2', at=6)
        self.assertEqual(str(ctx.exception), 'insufficient_org_credit')
        # 财务切换付款办法:先释放组织预留,t2 立即可得 70
        with self.tx() as db:
            settlement.release(db, 'inv1:org:g1', at=7)
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=7)
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=7)
            self.assertEqual(settlement.reserve(db, 'inv2', 'org', 70, 'g2', at=8)['status'], 'held')
            self.assertEqual(settlement.available_credit(db, 't2', at=8)['org'], 0)
        # t1 想切回组织信用:先释放预付与银行预留,再以原身份重建 60 → 组织只剩 10,拒绝
        with self.tx() as db:
            settlement.release(db, 'inv1:bank:x', at=9)
            settlement.release(db, 'inv1:prepaid:t1', at=9)
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv1', 'org', 60, 'g1', at=10)
        self.assertEqual(str(ctx.exception), 'insufficient_org_credit')
        # 同一结算身份(金额不变)释放后可重新建立预留;银行请求随之复活
        with self.tx() as db:
            self.assertEqual(settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=11)['status'], 'held')
            self.assertEqual(settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=11)['status'], 'held')
            self.assertEqual(store.row(db, 'SELECT status FROM bank_requests WHERE id=?', ('x',))['status'], 'pending')
            self.assertEqual(settlement.check_invariants(db), [])

    def test_concurrent_shared_credit_exactly_one_wins(self):
        self.setup_world()
        self.setup_t2()
        barrier = threading.Barrier(2)
        results = {}

        def attempt(label, invoice, grant, amount):
            try:
                barrier.wait(timeout=10)
                with store.transaction(self.db) as db:
                    settlement.reserve(db, invoice, 'org', amount, grant, at=5)
                results[label] = 'ok'
            except SettlementError as exc:
                results[label] = str(exc)

        threads = [threading.Thread(target=attempt, args=('t1', 'inv1', 'g1', 60)),
                   threading.Thread(target=attempt, args=('t2', 'inv2', 'g2', 70))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # 60+70>80:恰好一个成功,共享余额绝不透支
        self.assertEqual(sorted(results.values()), ['insufficient_org_credit', 'ok'])
        with self.tx() as db:
            self.assertEqual(settlement.check_invariants(db), [])
            held = db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE status='held'").fetchone()[0]
            self.assertIn(held, (60, 70))

    # ---------------------------------------------------------- guarantees

    def test_guarantee_rules_and_revocation_blocks_unsubmitted(self):
        self.setup_world()
        with self.tx() as db:
            settlement.grant_guarantee(db, 'g3', 'O', 't1', 10, expires=5, at=1)
        # 有效期外
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv1', 'org', 10, 'g3', at=10)
        self.assertEqual(str(ctx.exception), 'guarantee_expired')
        # 受益租户不符
        self.setup_t2()
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv1', 'org', 10, 'g2', at=3)
        self.assertEqual(str(ctx.exception), 'guarantee_tenant_mismatch')
        # 最高额度:60 已占,再占即超额
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'org', 60, 'g1', at=4)
        self.retry_job('j3')
        with self.tx() as db:
            settlement.issue_invoice(db, 'inv3', 't1', at=5)
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv3', 'org', 10, 'g1', at=6)
        self.assertEqual(str(ctx.exception), 'guarantee_cap')
        # 撤销拦下尚未提交的划转:held 不能捕获,只能释放
        with self.tx() as db:
            self.assertEqual(settlement.revoke_guarantee(db, 'g1', at=7)['decision'], 'revoked')
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.capture(db, 'inv1:org:g1', at=8)
        self.assertEqual(str(ctx.exception), 'guarantee_revoked')
        with self.tx() as db:
            settlement.release(db, 'inv1:org:g1', at=9)
            self.assertEqual(settlement.available_credit(db, 't2', at=9)['org'], 70)
            self.assertEqual(settlement.check_invariants(db), [])

    def test_late_revocation_uses_compensating_entries(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'org', 60, 'g1', at=3)
            settlement.capture(db, 'inv1:org:g1', at=4)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'settled')
        # 组织撤销迟到:划转已执行,只能补偿
        with self.tx() as db:
            decision = settlement.revoke_guarantee(db, 'g1', at=5)
            self.assertEqual((decision['decision'], decision['executed']), ('revoked_late', 60))
            # 已结清发票保持原样,历史不改写
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'settled')
            comp = settlement.compensate_guarantee(db, 'g1', at=6)
            self.assertEqual(comp, {'grant': 'g1', 'compensated': 60, 'recovered_prepaid': 30, 'receivable': 30})
            org = db.execute('SELECT balance FROM org_accounts WHERE org=?', ('O',)).fetchone()[0]
            prepaid_balance = db.execute('SELECT balance FROM prepaid_accounts WHERE tenant=?', ('t1',)).fetchone()[0]
            receivable = db.execute('SELECT COALESCE(SUM(amount),0) FROM receivables WHERE tenant=?', ('t1',)).fetchone()[0]
            # 组织追回 60;租户预付抵 30,余 30 记应收;无账户为负
            self.assertEqual((org, prepaid_balance, receivable), (80, 0, 30))
            # 补偿幂等
            self.assertEqual(settlement.compensate_guarantee(db, 'g1', at=7)['compensated'], 0)
            self.assertTrue(settlement.verify_invoice(db, 'inv1')['consistent'])
            self.assertEqual(settlement.check_invariants(db), [])

    # ---------------------------------------------------------- reversal

    def test_wrong_invoice_reversal_restores_all_sources(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=3)
            settlement.reserve(db, 'inv1', 'bank', 30, 'x', at=3)
            settlement.capture(db, 'inv1:prepaid:t1', at=4)
        settlement.submit_bank(self.db, self.bank, 'x', at=5)
        with self.tx() as db:
            settlement.capture(db, 'inv1:bank:x', at=6)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'settled')
        # 已发错账冲正:预付退回、银行真实退款、计量行回到未开票池
        with self.tx() as db:
            settlement.reverse_invoice(db, 'inv1', at=7, reason='misissued', bank=self.bank)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'reversed')
            self.assertEqual(tuple(db.execute('SELECT balance, held FROM prepaid_accounts WHERE tenant=?', ('t1',)).fetchone()), (30, 0))
            self.assertTrue(settlement.verify_invoice(db, 'inv1')['consistent'])
            self.assertEqual(settlement.check_invariants(db), [])
        self.assertEqual(self.bank.net('t1'), 0)
        self.assertEqual([t['kind'] for t in self.bank.ledger()], ['debit', 'refund'])
        # 冲正后可从原件重开正确发票并再次结清
        with self.tx() as db:
            self.assertEqual(settlement.issue_invoice(db, 'inv1b', 't1', at=8)['ticks'], 60)
            settlement.reserve(db, 'inv1b', 'org', 60, 'g1', at=9)
            self.assertTrue(settlement.capture(db, 'inv1b:org:g1', at=10)['settled'])
            self.assertEqual(settlement.check_invariants(db), [])

    def test_reverse_open_invoice_releases_holds_without_money_flow(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=3)
            settlement.reverse_invoice(db, 'inv1', at=4, reason='cancelled_order')
            self.assertEqual(tuple(db.execute('SELECT balance, held FROM prepaid_accounts WHERE tenant=?', ('t1',)).fetchone()), (30, 0))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ledger').fetchone()[0], 0)
            self.assertEqual(settlement.issue_invoice(db, 'inv1c', 't1', at=5)['ticks'], 60)
            self.assertEqual(settlement.check_invariants(db), [])

    # ---------------------------------------------------------- integration & limits

    def test_existing_meter_invoice_prepaid_modules_integrate(self):
        # 既有链路:按页摘要采录 → 临时发票 → 预付账户;结算层接管同一发票
        with self.tx() as db:
            prepaid.open_account(db, 't1', 100)
            usage_capture.import_page(db, 'p1', [{'node': 'n1', 'tenant': 't1', 'job': 'j1', 'ticks': 20}])
            invoice_module.issue(db, 'i-old', 't1')
            settlement.ensure_schema(db)
            settlement.reserve(db, 'i-old', 'prepaid', 20, 't1', at=1)
            self.assertTrue(settlement.capture(db, 'i-old:prepaid:t1', at=2)['settled'])
            st = settlement.invoice_status(db, 'i-old')
            self.assertEqual((st['status'], st['ticks']), ('settled', 20))
            self.assertEqual(db.execute('SELECT balance FROM prepaid_accounts WHERE tenant=?', ('t1',)).fetchone()[0], 80)
            self.assertEqual(settlement.check_invariants(db), [])

    def test_over_invoice_and_negative_balances_rejected(self):
        self.setup_world()
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'prepaid', 30, 't1', at=3)
        # 预留合计不得超过发票总额
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv1', 'org', 31, 'g1', at=4)
        self.assertEqual(str(ctx.exception), 'over_invoice')
        with self.tx() as db:
            settlement.reserve(db, 'inv1', 'org', 30, 'g1', at=4)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['outstanding'], 0)
        # 子租户不得为负:t1 的 30 已全部预留
        self.retry_job('j3')
        with self.tx() as db:
            settlement.issue_invoice(db, 'inv3', 't1', at=5)
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv3', 'prepaid', 10, 't1', at=6)
        self.assertEqual(str(ctx.exception), 'insufficient_prepaid')
        # 结清后拒绝任何新预留
        with self.tx() as db:
            settlement.capture(db, 'inv1:prepaid:t1', at=7)
            settlement.capture(db, 'inv1:org:g1', at=8)
            self.assertEqual(settlement.invoice_status(db, 'inv1')['status'], 'settled')
        with self.assertRaises(SettlementError) as ctx:
            with self.tx() as db:
                settlement.reserve(db, 'inv1', 'bank', 1, 'y', at=9)
        self.assertEqual(str(ctx.exception), 'invoice_not_open')
        with self.tx() as db:
            self.assertEqual(settlement.check_invariants(db), [])


if __name__ == '__main__':
    unittest.main()
