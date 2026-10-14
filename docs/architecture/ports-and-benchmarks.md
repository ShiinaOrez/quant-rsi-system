# 可替换存储与 Benchmark 的边界

RSI 核心面向“候选模型在版本化任务上的表现”，不应理解股票、撮合、SQLite、Tinker 或某个外部评测框架。采用依赖注入和端口/适配器结构，先保持 Python 模块化单体。

## 存储契约

| 端口 | 语义 | 当前实现 | 后续替换 |
| --- | --- | --- | --- |
| TrajectoryRepository | 按 ID 原子保存完整轨迹、读取；同内容重试幂等，不同内容冲突 | SQLite、内存 | Serverless DB 适配器 |
| ArtifactStore | 写入 bytes 得 SHA-256，读取时校验内容 | 本地文件 | 对象存储适配器 |
| EvaluationRepository | 按 evaluation ID 保存不可变结果 | 内存 | SQLite/Serverless DB 适配器 |
| EpisodeRepository | 交易 episode 事务持久化 | 接口预留 | 数据库适配器 |

数据库路径、连接、表、SQL 和供应商客户端只存在于适配器。JSONL 导出通过 TrajectoryRepository 读取，再调用独立序列化 codec；任何符合端口的仓库都会得到同样的导出字节。SQLite 的原 export_jsonl 方法仅保留兼容转发。

Serverless 实现必须用事务、条件写入或等价机制满足“同 ID 不覆盖”的原子语义，不能采用没有并发保护的先查后写。远程后端的超时、连接异常和重试映射放在适配器；应用不根据 provider 名称分支。未来异步接口变更需作为端口版本升级，而不是让同步接口直接返回 coroutine。

本文保证的是可替换契约，不是声称不同数据库成本、一致性和延迟相同。当前没有实际 Serverless DB 适配器。对象存储和数据库不能跨系统原子提交：先写不可变工件，再提交数据库引用；失败产生的孤立工件可由后台回收，不在调用过程中删除共享工件。

## Benchmark 契约

`BenchmarkIdentity(suite_id, version)` 标识评测套件；`BenchmarkCase` 携带 case ID、split、配置和数据哈希。`EvaluationRequest` 再绑定 run、evaluation ID 与 policy revision。`BenchmarkOutcome` 返回指标、可选 success 和已持久化轨迹 IDs。

Metric 保存 name、Decimal value、unit 与 direction（maximize/minimize/diagnostic），支持收益、准确率、成本或框架原始指标。没有 success 判定的 Benchmark 可以返回 None。不能把准确率 0.8 和收益率 0.1 直接比较或平均；RSI 的晋级策略需要显式指定套件、版本、指标和阈值，后续独立实现。

`BenchmarkRunner` 执行自己负责的任务，对自身的输入、模型调用、时序和 scoring 负责。RSI 的 `EvaluateCandidate` 根据完整 suite/version 找到 runner，校验返回 provenance，再经注入的 repository 保存结果。它不加载 TOML、不产生股票订单、不读取 SQL。

`TradingReplayBenchmark` 是当前交易领域的适配器，把现有 replay 的结果映射成通用指标；其内部才知道 TradingSession、MarketHistory 和 TraderPolicy。未来 Finance Agents Benchmark 或其他评测通过自己的 runner 接入，自行把 checkpoint/model agent 转成其要求的接口、把原始分数转成 Metric；不要强迫外部套件实现我们的 TargetWeight 或使用 60 日账户。外部评测框架尚未接入，本次使用非交易 fixture 证明端口可替换。

通用 DatasetSplit 已移至 shared，轨迹与后训练不再依赖具体交易 Benchmark 模块；原 benchmark 模块保留重导出以兼容已有调用。

## 组装与权限

```python
# 仅 composition root 选择具体实现。
repository = SQLiteTrajectoryStore(database_path)
artifacts = LocalArtifactStore(blob_root)
evaluation_repository = InMemoryEvaluationRepository()
rsi_evaluator = EvaluateCandidate(
    runners=(trading_runner, external_benchmark_runner),
    repository=evaluation_repository,
)

# 换成 ServerlessTrajectoryRepository(...) 后，导出用例无需修改。
digest = archive_export(repository, artifacts, trajectory_id)
outcome = rsi_evaluator.execute(request)
```

这是组装示意，不是已运行的外部框架。`EvaluateCandidate` 拒绝 sealed test 请求；最终测试属于独立评估用例，不把结果交给 Researcher。权限网关尚未实现，此检查不能替代服务端访问隔离。

测试覆盖 SQLite/内存相同仓库契约和逐字节 JSONL 导出、一套 RSI 用例同时执行交易与非交易套件、错误版本和封存测试拒绝、返回错误模型版本时不保存结果。现有 replay 和配置兼容性测试继续保留。
