# 期限感知批作业编排庭

供离线云平台的批作业运营员判断期限承诺、执行槽租约和产物交付。SQLite 保存作业与依赖、槽与租约、尝试与产物、撤销及保留证据。三个子系统分别由 `job`、`slot`、`run` 命令调用；故障回执会改变后续承诺或交付结论。

## 许可证席位（软件席位，不是 CPU 槽）

商业求解器按并发席位授权，席位是否可用由许可证服务决定，与执行槽的 CPU/内存容量相互独立。`license` 子系统把整条可恢复的业务边界落在 SQLite 与供应商网关之间：

- 权利校验按产品、版本、地域、租户与可借时段（窗口）进行；客户自带合同（`byol`）只服务本租户，运营方备用合同（`operator`）可服务任意租户但租金记运营方。一个尝试跨两个时段时必须保持同一有效权利，或在边界上 `license.reauthorize` 重新获准。
- 席位占用只在执行机从供应商**实际取得令牌**时开始（`license.borrow`），到供应商确认归还（`license.checkin`）或合法超时失效（`license.timeout`，附不可变纠正记录）才结束。本地删除行会让交付失去证据，不会释放远端席位。
- 供应商离线时：借用响应丢失只产生 `quarantined` 待核实记录，绝不向第二份合同重复申领；无法证明空闲的席位保持占用或隔离，只能经审计的 `license.writeoff` 处置。运营方可 `license.emergency` 从备用保留席位人工借令牌，责任记运营方并保留原始回单。
- 迟到回执（`license.receipt`）不能让已到期、已取消或旧代次的尝试重新合法，只作为 `stale` 原始证据留存；供应商事后纠错（`license.correction`，如重复扣占退款）只追加不可变记录，不覆盖历史。
- 作业的许可证需求在提交时单独登记；父作业的授权不自动覆盖子作业。交付摘要通过 `delivery_evidence` 指回所用令牌与借用回单，`license.audit` 输出每次调用的完整合规链。

## 运行

无需第三方依赖。安装检查：`python -c "import cloudctl; print(cloudctl.__file__)"`。

```sh
python -m cloudctl init --db state.sqlite
python -m cloudctl demo --db state.sqlite --fixture fixtures/demo.json
python -m cloudctl demo --db state.sqlite --fixture fixtures/许可证席位.json
python -m unittest discover -s tests -v
```

重新演示时删除演示数据库，测试使用独立临时数据库。`fixtures` 包含各子系统的正常和失败输入。CLI 每行输出决定、事件及证据。状态变更在 SQLite `BEGIN IMMEDIATE` 事务中提交；失败与旧代次不会伪装成交付，无法证明的软件席位不会伪装成空闲。
