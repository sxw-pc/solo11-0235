# 期限感知批作业编排庭

供离线云平台的批作业运营员判断期限承诺、执行槽租约和产物交付。SQLite 保存作业与依赖、槽与租约、尝试与产物、撤销及保留证据。三个子系统分别由 `job`、`slot`、`run` 命令调用；故障回执会改变后续承诺或交付结论。

## 运行

无需第三方依赖。安装检查：`python -c "import cloudctl; print(cloudctl.__file__)"`。

```sh
python -m cloudctl init --db state.sqlite
python -m cloudctl demo --db state.sqlite --fixture fixtures/demo.json
python -m unittest discover -s tests -v
```

重新演示时删除演示数据库，测试使用独立临时数据库。`fixtures` 包含三个子系统的正常和失败输入。CLI 每行输出决定、事件及证据。状态变更在 SQLite `BEGIN IMMEDIATE` 事务中提交；失败与旧代次不会伪装成交付。

## 申诉审定与补救（cloudctl/claims.py）

申诉把 **一个** 已接受的作业承诺与受理时的作业版本、期限和合同版本一起钉住（`claims` 表）；之后的期限修订、旧代次回执或撤销后的重放都不会改写责任起点。生命周期动作（`claim.*`，同时接受 `claims.*` / `appeal.*` 等近义动作名）：

- `claim.open`（`file/accept/intake`）受理并钉住承诺，相同申诉身份重放为 `idempotent`，指向不同承诺为 `conflict`。
- `claim.evidence`（`supplement/add_evidence`）按证据版本追加原始证据（`claim_evidence` 表）；相同证据号或相同摘要的补传只记一次。
- `claim.adjudicate` / `claim.audit`（`decide/rule`）只依据槽健康、租约、尝试、父作业交付与可核验客户操作给出 `platform_liable`、`customer_liable`、`parent_undelivered`、`not_breached` 或 `inconclusive`。单凭心跳缺口永远是 `inconclusive`。
- `claim.select` 与 `claim.remedy`（`execute/remediate`）在同一 `BEGIN IMMEDIATE` 事务内锁定补救选择并落实：
  - 平台责任 → `rerun`：用既有 `slots.lease` 检查备用槽健康、容量、期限与占用，写 `slot_reservations`（部分唯一索引保证一个备用槽同时只有一个 `held` 预留），把补跑租约标记为受保护（不可抢占），再走既有 `run.start` / `run.settle` 真实交付；交付钩子恰好一次地兑现补救、释放租约与预留，可幂等重试。`reschedule` 按申诉把增量期限一次性平移给受影响后继作业。
  - 客户责任 → `credit`：从 `time_balances` 已购买未使用额度签发一张可追溯、可转让、单次使用的 `credits` 凭证（确定性 id + 唯一幂等键）。`credit.redeem` 用条件 `UPDATE ... WHERE status='available'` 保证并发/重试下只兑现给下一笔真实作业一次；`credit.transfer` 支持转让。
- `claim.revoke`（`cancel/withdraw`）撤销并冻结申诉、释放未兑现预留；撤销后的补证、裁定、补救与同身份重放都不会复活它。
- `claim.report` / `claim.sync` 返回完整证据、补救、凭证与按 `seq` 排序的事件流。

所有决定都写入既有 `events` 表（`subject=申诉 id`），可用 `store.report(db, claim_id)` 或 `execute(..., {'action':'claim.report','id':...})` 审计完整顺序与原始证据。并发受理、两个申诉争抢同一备用槽、凭证并发核销均有测试覆盖：`python -B -m unittest tests.test_claims -v`。
