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
