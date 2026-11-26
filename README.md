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

## 物理节点容量与可恢复租约

同一物理节点上的多个执行槽共享同一份真实 CPU/内存，不能按槽重复出售。

- `node.register` 登记或降额节点的真实物理容量；槽首次登记时按槽规格自动建节点。节点容量按**真实重叠分钟**聚合，换槽号或换租户都不能重复出售，超出即回 `node_overcommit`。
- 每条租约在落地前由 `planner.feasible` 重放整条时间线：固定租约按分钟占用物理容量，未租约工作与抢占债务作为灵活工作按 EDF 排布，并守住父依赖与每个期限。抢占只有在剩余债务仍能在原期限内兑现时才落地，否则回 `timeline_infeasible` 且不改动任何既有状态。
- 抢占按实际已运行分钟结算债务（`debt`），已完成的 attempt 与产物永不删除、不可改写成“从未执行”；旧代次的迟到成功回 `stale_attempt`，不能抢走新代次交付。`slot.repay` 为债务安排补偿槽，`slot.release` 由运营员交还预留。
- 心跳隔离立即把租约置为 `isolated` 并停止其容量承诺；心跳恢复不会复活旧代次，租约转为 `fenced`。节点降额、断电重试与审计回放都保留同一份已成功产物与物理占用。
- 父任务未交付时子任务可预留但不可交付；父任务取消或改期会重估整条时间线，无法兑现的子租约被释放并留下 `release` 事件。旧版数据库只做增量迁移（新增 `nodes` 表与 `leases.granted` 列），历史事件与产物保持可审计。

