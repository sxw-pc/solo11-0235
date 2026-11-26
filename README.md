# 期限感知批作业编排庭

供离线云平台的批作业运营员判断期限承诺、执行槽租约和产物交付。SQLite 保存作业与依赖、槽与租约、尝试与产物、撤销及保留证据。三个子系统分别由 `job`、`slot`、`run` 命令调用；故障回执会改变后续承诺或交付结论。

## 密钥轮换与应急撤销

`cloudctl/secret_rotation.py` 在原有一次性封装登记之上，把客户密钥的轮换和应急撤销接入挂载、租约、运行与审计流程。原始密钥字节从不进入本地数据库或事件，只保存封装编号、密文摘要、外部密钥服务回执、容器证明和产物摘要。

- 每个挂载绑定租户、作业、租约代次、密钥版本和短时有效范围（TTL 上限 300 秒且不超过租约窗口）。
- 轮换只登记新版本与轮换意向，不直接发挂载；旧挂载必须取得密钥服务回收确认、租约隔离证据或到期证据后，新机才能领到写入权，杜绝两台执行机同时持有同一作业的写入权。
- 撤销按租户作用域冻结对应版本的挂载；读取过被撤销版本的代次被标记污点，迟到的旧代次成功回执只能进入隔离产物，不能对客户交付。撤销不会沿父子依赖边影响其他租户的密钥。
- 两条恢复路径均从同一条超时回执出发：路径 A 凭“容器未接触泄露材料”证明加密钥服务回收确认，在客户同意的恢复点用新版本续接并保留前半段计算血缘；路径 B 在旧机离线无回收证明时隔离租约、废弃含旧版本秘密的临时字节，由备用机用新版本重新计算。
- 撤销原因、密钥服务回执（重复回执幂等）、容器证明、运行尝试、恢复点血缘和交付资格共同组成可复演证据链，可通过 `secret.evidence` 重放。

相关命令：`secret.register`、`secret.mount`、`secret.rotate`、`secret.revoke`、`secret.kms_receipt`、`secret.attest`、`secret.recovery_resume`、`secret.quarantine`、`secret.standby_mount`、`secret.confirm_recompute`、`secret.evidence`。

## 运行

无需第三方依赖。安装检查：`python -c "import cloudctl; print(cloudctl.__file__)"`。

```sh
python -m cloudctl init --db state.sqlite
python -m cloudctl demo --db state.sqlite --fixture fixtures/demo.json
python -m unittest discover -s tests -v
```

重新演示时删除演示数据库，测试使用独立临时数据库。`fixtures` 包含三个子系统的正常和失败输入。CLI 每行输出决定、事件及证据。状态变更在 SQLite `BEGIN IMMEDIATE` 事务中提交；失败与旧代次不会伪装成交付。
