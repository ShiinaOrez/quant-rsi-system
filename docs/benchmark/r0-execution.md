# R0 准入与执行

R0 使用固定的本地 Qwen、全部 20 题、三个基线和已有轨迹端口。它不要求训练集、Tinker、DeepSeek 或 Supabase。训练全流程的 preflight 不作为 R0 的准入入口。

用户已经要求补齐完整档案；本轮保持该要求。共享数据计划中 `block_materialization_until_coverage_disclosed` 允许保存带缺口的构建产物，不代表披露缺口就能获得正式成绩。缺口档案可以编译和测试，但正式执行仍检查完整性证据。

## 当前入口

```sh
.venv/bin/python -m scripts.r0_readiness \
  --output artifacts/benchmark-quality/r0-readiness-new

.venv/bin/python -m scripts.run_r0 --execute \
  --output artifacts/runs/r0-new
```

输出目录必须尚不存在。准入检查读取固定版本的参考索引、已保存的价格窗口检查和明确列出的来源缓存。它核对原始字节摘要，逐题列出截止日、终点、参考申报、已取得内容范围、最新年报与季报及阻断项。未纳入清单不表示文件不存在；先核对既有缓存，再采集。

套件有任一题未准入时，执行命令保存 `blocked_before_inference`、缺失分数和 `model_calls=0`，退出码为 2，不访问模型或数据库。不会跳过未准入题或把 pilot 改称 R0。

## 冻结档案

`FrozenArchiveMarket` 实现 `MarketHistory`、`SessionCalendar`、`InformationArchive` 三个端口，不依赖数据库或采集平台。`compile_archive` 接收已经归一化的数据，不负责从任意 HTML 中猜测正文、恢复价格或确认许可。这些转换和复核必须有自己的来源证据。

构建描述使用 TOML，记录数据使用 JSONL：

```toml
schema_version = "frozen_archive_build.v1"
task = "task.toml"
records = "records.jsonl"
sessions = "sessions.json"
admission = "admission.json"

[[evidence]]
bundle_path = "evidence/source.bin"
source_path = "source.bin"
```

```sh
.venv/bin/python -m scripts.compile_archive --spec build.toml --output bundle-new
```

每条输入记录提供 `record_id`、`symbol`、`kind`、`event_time`、`content`、`source`、`raw_sha256`、`availability_input`。财务事实还须提供 `filing_accession` 和 `originating_record_id`；其可见时间继承原始文档。编译器补充时间策略结果，复制并绑定来源证据，生成记录、交易日、准入报告及其摘要。已有目录不会覆盖，失败的构建保留供排查。任务不会被自动标为 ready。

`manifest.json` 绑定标的、题目、时钟、周期、引擎和每个文件的 SHA-256。加载时重新验证全部字节、实际可见时间和 XNAS 开闭市时间。代理历史端口只暴露记录正文及基本来源，不暴露全档案统计、未来交易日数据、准入报告、拆股还原因子或 assessor 材料。

## 可见时间

- `verified_release.v1`：使用显式、带证据摘要的 `published_at`。
- `publication_date_next_open.v1`：只知道公开日期时，推迟到该日期之后的第一个交易日开盘。
- `filing_date_next_open.v1`：按固定参考索引中的 assigned filing date 推迟到随后第一个交易日开盘。这是明确标记 `estimated=true` 的保守代理，不是已验证的公开秒数；日期证据必须绑定，不能从受理时间或存档捕获时间推导。

后两项中可附受理元数据，候选边界早于受理时间时拒绝构建，要求重新复核。只有受理时间不足以建立可见性。加载器会重新计算策略结果，不能只修改序列化的 `available_at` 提前开放资料。来源字节是当前版本还是历史原件仍须复核，保守延迟不能修复错误版本。

## 正式准入

准入报告采用 `archive_admission.v1`，逐项给出 `passed`、`blocked` 或 `not_reviewed` 和单独绑定的 `evidence_paths`：

| 检查 | 所需证据 |
|---|---|
| `filings_history` | 固定参考索引及逐份主文档绑定 |
| `filing_exhibits` | 每份申报的完整文件集及列表依据 |
| `financial_statements` | 三表、单位、期间、维度和修订的复核报告 |
| `market_history_and_actions` | 价格口径、历史指标输入、公司行动及账务复核 |
| `permitted_use` | 实际来源和使用范围的人工复核 |

这五项包含人工判断，哈希匹配不证明其结论正确。编译器不生成虚假的“审核通过”材料。除了检查报告和证据真实绑定，加载器还会：

1. 从绑定的参考索引计算截至题目终点的六类表单集合，与 `filing_coverage.v1` 的逐份记录比对，拒绝空集合、缺项和重复项。R0 检查器再将索引摘要与套件固定版本核对。
2. 核对每份主文档可经历史接口查询、accession 对应、原始字节属于所列文件集；附件完整性必须有单独的列表依据。支持发行人镜像文件集，不要求全部重新通过 SEC 主机获取。
3. 检查实际 60 个交易日、每次决策的收盘 bar，以及正式档案的至少 252 个交易日预热。当前引擎无法处理的公司行动会明确拒绝。

通过后才能将生成的 manifest 路径和摘要绑定到任务并标记 ready；运行器仍会重新加载验证，手改状态不能替代证据。

## 执行和重放

运行器先加载全部 20 题并执行 cash、buy-and-hold、SMA20/60 基线，再访问固定版本的本地 Qwen。模型前后均检查 digest；保存运行配置、关键实现文件的摘要、每次模型请求/响应、工具查询、账务、逐题结果和 JSONL 轨迹。SQLite/CAS 只是 CLI 默认实现，执行函数依赖注入轨迹与制品端口。

当前工具配置在 R0 前固定为 6 次查询、12,000 UTF-8 字节预算，`require_history_evidence=false`，与既有默认行为一致。模型可自主查询；这不是根据成绩调整的设置。采样沿用既有协议的 temperature=0、seed=42、8192 context、192 output tokens 和关闭 thinking；不一致的运行配置会被拒绝。

每个已完成任务都使用保存的模型输出重新执行工具调用，比较完整请求、工具记录和账务；重放不调用模型。模型协议失败保留缺失收益和失败轨迹，基础设施/重放失败单独记录，不能填零收益或报告正式总分。只读源码和合成夹具测试不属于 R0 成绩。
