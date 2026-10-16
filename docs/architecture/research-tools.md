# Researcher 工具契约 v1

初版实现日期：2026-10-16。六个业务工具可由 Python 调用，也可通过官方 MCP SDK 的 stdio 服务暴露给 DSH。JSON Schema 同时用于模型发现、输入校验和输出校验，唯一来源为 [research-tools.v1.json](../../quant_rsi/tools/research-tools.v1.json)。本轮已用官方 MCP 客户端跑通全流程，没有调用 DeepSeek、付费训练或再次运行真实 Qwen 回测。

## 模型应遵循的流程

```text
search_evidence → read_evidence（每个被引用的证据）
    → propose_experiment → submit_candidate
    → request_experiment → get_experiment
    → read_evidence（结果证据）→ 下一项假设 / 消融 / 复测
```

每个工具的 description 包含步骤、前置条件、实际副作用与下一步；每个参数解释如何获得 ID 和应该表达什么。输入对象禁止额外字段，不接受模型提供的数据摘要、数据库路径、执行命令、评测器或预算覆盖值。输出包含 `ok`、`tool_schema_version`、`campaign_id`、`data/error` 和 `next_actions`；后者给出下一工具、原因和已有参数。部分参数仍需模型补充，不能把 next_actions 当成自动调度指令。

| 工具 | 前置条件 | 结果与副作用 |
| --- | --- | --- |
| `search_evidence` | 无 | 查询主机登记的许可索引，不做实时 Web 搜索；返回证据摘要、基线与候选、可用题目、支持的候选类型和预算。 |
| `read_evidence` | 来自发现结果的证据 ID | 读取许可视图并记录已检查的证据；validation 只返回批准摘要，test 不可见。 |
| `propose_experiment` | 引用的证据都已读，父候选/对照/题目均已绑定 | 保存不可变提案：假设、证伪条件、单组件修改、对照、消融/确认计划及解释标准。不执行。 |
| `submit_candidate` | 已有提案 | 继承提案的父版本和候选类型，保存完整 UTF-8 工件并返回 SHA-256。相同提交幂等；与对照完全相同的工件被拒绝。不执行。 |
| `request_experiment` | 候选必须属于该提案 | 原子预留预算并入队。每题执行一次对照与一次候选，因此占两个 evaluation slot。稳定幂等键的重复请求返回同一 attempt。 |
| `get_experiment` | 已有 attempt ID | 返回状态、成对指标、错误或结果证据 ID。不触发执行或晋级。 |

输入/输出 Schema 都提供给 MCP 客户端；DSH 对输出 Schema 的提示呈现由其版本决定，因此重要语义也写在工具 description 与服务 instructions 中，不只依赖 outputSchema。

## 状态、失败与评估语义

执行状态为 `queued → running → completed | failed`。`completed` 只表示执行完毕，每个 Benchmark 的 `success` 仍可能为 false。消融计划、确认计划和叙述性的 success_criteria 是已记录的研究设计，不会自动变成额外执行或机器判定；真正的成功规则来自原 Benchmark。

错误区分无效参数、未读证据、未绑定提案/候选、候选归属错误、未就绪数据、预算不足和幂等键冲突。失败执行返回 `failed` 和脱敏错误类别，不生成零分“成功结果”，也不返回提供商异常原文。`retryable=false` 表示不建议原样自动重试；修正参数或补齐前置条件仍可重新调用。

worker 通过现有 `EvaluateCandidate` / `BenchmarkRunner` 为同一冻结题目评估对照与候选。每个结果保留 suite/version、配置/数据摘要、policy revision、指标 Decimal 字符串、单位和方向，不跨不同套件平均分数。结果及失败记录会成为可检索证据，但机制是否成立、是否泛化仍保持未确认。

当前只绑定 prompt 类型的执行器；Schema 保留其他文本候选类型供后续受控执行器注册。发现结果中的 `supported_candidate_kinds` 才是当前能力，不支持的类型在提案时拒绝。没有训练后端、自动晋级、GEPA 搜索或任意代码执行能力。

## DDD 与持久化边界

`ResearchCampaign` 管理证据访问、提案、候选、执行状态和预算规则；应用 `ResearchService` 处理六个用例及 worker。MCP/JSON Schema 不进入领域；SQL 不进入应用。ResearchRepository 与 ArtifactStore 可分别替换。

已实现内存与 SQLite 事务仓库。SQLite 使用 BEGIN IMMEDIATE 原子处理状态转换、幂等和预算预留；外部实验在提交 claim 之后、数据库事务之外执行。候选内容由原有 SHA-256 工件仓库保存。数据库重启后可读取提案、候选、预算与最终结果；持久化格式为 `research-state.v1`。

已接受的失败任务仍占预算，避免反复重试绕过限制。初版不自动重放 running 状态：进程中断后执行效果可能不确定，需要主机核查，而不是由模型换一个幂等键偷偷重跑。记录请求、开始、结束的真实时间，区别于交易模拟时钟。当前预算覆盖 attempt 和 Benchmark evaluation 次数；Researcher token、训练费用及全局运行时间预算仍需后续控制器实现。

Researcher 通过工具看到的是受控视图；这不等于给整个 DSH 进程建立了操作系统隔离。所附 patch 只增加 MCP 工具，不关闭 DSH 原有 shell 或文件访问；专用 profile 和隔离执行环境仍是部署工作。

## 安装与可执行示例

在仓库根目录，Python 3.11+：

```sh
.venv/bin/python -m pip install -e '.[research]'
.venv/bin/python -m scripts.demo_research_tools --root artifacts/research-demo-001
.venv/bin/python -m unittest discover -s tests
```

示例用官方 MCP 客户端启动真实 stdio 子进程，依次调用全部六个工具。执行器使用两日人工行情和固定仓位，结果明确标记 `synthetic_demo`；`demo:weight=1` 只是 demo 的 prompt 工件格式，不代表有模型在推理。目录中保存 `research.sqlite3`、`blobs/`、`mcp-transcript.json`、`result.json`。重复运行示例会创建新提案并花费剩余 demo 预算；需要全新实验时使用新目录。

只启动 MCP 服务：

```sh
.venv/bin/python -m scripts.research_server --mode demo --root artifacts/research-demo-001
```

demo 有应用后台 worker。真实历史 pilot 使用显式模式，并由独立应用进程处理队列：

```sh
# MCP 服务，仅接收请求、查询结果
.venv/bin/python -m scripts.research_server --mode local-pilot \
  --campaign-id intc-research-pilot --root artifacts/intc-research-pilot

# 在另一个终端由主机主动处理一个队列任务；会实际运行两轮 60 日本地 Qwen 评估。
.venv/bin/python -m scripts.research_server --mode local-pilot \
  --campaign-id intc-research-pilot --root artifacts/intc-research-pilot --work-once
```

local-pilot 复用现有冻结 INTC bundle，保留 validation 与数据未就绪标记，只做诊断；不产生后训练数据。执行前检查本机模型摘要与已冻结的 Qwen 配置相符。相同 campaign 的数据/基线/预算绑定不能在重启时悄悄替换。本轮未执行此真实 pilot 模式。

## DSH 接入

所附 [Cordis patch](../../configs/researcher/dsh-research-tools.patch.yml) 使用 DSH 已审查的 MCP client 接口。由主机提供路径：

```sh
export QUANT_RSI_REPO="$PWD"
export QUANT_RSI_PYTHON="$PWD/.venv/bin/python"
export QUANT_RSI_RESEARCH_ROOT="$PWD/artifacts/dsh-research-demo"
```

将 patch 交给 DSH 的配置层或 Python SDK 的 `patches=(absolute_patch_path,)`。DSH 预期工具名为 `mcp__quant_research__search_evidence` 等；服务端原始名称保持六个业务名称。patch 默认连接 demo，不自动切到真实数据或训练。MCP 客户端层已端到端验证，DSH 模型实际调用和 profile 挂载尚未实测；凭据不包含在此 patch 中。

协议与适配参考：[官方 MCP Python SDK v1.30.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)、[DSH MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/571902028a75c64c1d767f4ae5fd099485f0ddf0/packages/mcp/mcp-client/README.md)。
