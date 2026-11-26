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

## 谱系证明与父副本清理

每份已交付产物都在 `deliveries` 表留下不可变的交付记录，钉住摘要、成功尝试代次、交付事件与依赖版本闭包。凭据不足时，仍可独立审计子产物，而不依赖父产物的在线字节。

`run.seal` 在子产物交付后，把每个直接父边的父摘要、成功尝试代次、交付事件和传递依赖闭包封成带 HMAC 签名的不可变证据（`lineage_seals`，只追加）。`job.verify_lineage` 与 `job.audit` 可在父字节清理后重放该封包。

`run.purge` 的规则是：父副本自身无保留义务、且每位仍在保留期的已交付子孙都已封存并通过校验时，才真正释放父的在线字节；否则推迟并在决定日志中记录原因。父被多个子共享时，任一子缺封包都会阻止清理。

封存、保留与清理都写入 `decisions` 哈希链（含前一条摘要），断电或并发交付只能向后追加；`verify_decisions` 可发现删改或断裂。旧库无需迁移：新增表在事务内按需创建，原有交付、保留与事件表原样保留。
