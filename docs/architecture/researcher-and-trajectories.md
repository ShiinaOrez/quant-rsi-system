# RSI Researcher 与轨迹存储选型

日期：2026-10-14。Researcher 使用 DeepSeek Harness 和 DeepSeek 模型，trader 仍为 Qwen3-8B。二者在用途、权限和训练数据上分开。

## Researcher 接入

DeepSeek Harness 是可配置模型和工具的开源 agent runtime，并保留 session 事件日志。[官方说明](https://www.deepseek.com/en/harness/)

本轮未在 DeepSeek 官方公开价格页找到名为 Coding Plan 的订阅，官方 API 展示按 token 计费。[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)

可核验的套餐路线为**火山方舟 Coding Plan + deepseek-v4-pro**。方舟官方支持列表包含该模型，并提供 DSH 接入入口；兼容 OpenAI 的套餐 Base URL 为 `https://ark.cn-beijing.volces.com/api/coding/v3`。使用固定模型 ID，而非随控制台切换的 `ark-code-latest`。DSH 的 llm-pi-ai 支持配置 OpenAI-compatible custom provider。这里记录的是选型，未购买套餐、未安装到用户环境或测试真实调用。[套餐说明](https://docs.volcengine.com/docs/ark/coding-plan-personal-plan-overview?lang=zh)、[DSH 接入](https://docs.volcengine.com/docs/ark/coding-plan-personal-ai-deepseek-harness?lang=zh)、[端点](https://docs.volcengine.com/docs/ark/coding-plan-personal-get-started?lang=zh)

项目配置：[deepseek-harness.toml](../../configs/researcher/deepseek-harness.toml)，不是 DSH 原生 settings 文件。后续适配器将其编译为当前 Harness 的配置；密钥只引用环境变量，不写进配置或轨迹。Harness 源码核验 commit 已记录，套餐模型别名可能更新，应在每个 run 记录实际返回的 model 与服务信息。

Researcher 读取训练轨迹及允许的验证摘要，产出假设、候选代码 diff、训练参数和实验计划。它不能修改冻结的 Benchmark 规则或访问封存测试反馈。研究活动的代码执行/训练启动由应用控制；配置中的权限字段尚不等价于已部署的 sandbox。Coding Plan 仅用于 researcher 的研发请求，不用于批量 trader rollout；部署前需核验所选套餐对自动化任务、工具及额度的实际支持。

## 存储组件决策

| 用途 | 选择 | 状态 |
| --- | --- | --- |
| 权威轨迹记录和查询索引 | SQLite，标准库 sqlite3 | 已实现完整轨迹原子归档 |
| 调试、交换、训练数据导出 | UTF-8 JSONL | 已实现带哈希尾记录的导出 |
| 原始 Harness 日志、长上下文、财报与张量文件 | 本地 SHA-256 工件目录 | 已实现按内容寻址存取 |
| 大批量指标和 token 分布分析 | Parquet + DuckDB | 后续派生层，尚未实现 |

20 道题规模先使用本地数据库，不增加远程服务。SQLite 是权威来源，JSONL/Parquet 均为可重建导出，避免数据库与文件双写不一致。一次保存完整已结束轨迹的事务；重复保存相同 ID/内容幂等，内容不同拒绝覆盖。活动中的 DSH session 原始日志由 Harness 自己持久化；交易 episode 的逐步崩溃恢复和实时事件采集尚待接入。

初始使用 DELETE journal + FULL synchronous、数据库事务串行化写入。以后需要 WAL 并发读写时，先核验 SQLite 版本与部署方式；官方说明 WAL 同一时刻仅有一个 writer，且依赖同机共享内存。当前不把数据库放在网络共享盘。[SQLite 文档](https://www.sqlite.org/wal.html)

Parquet 适合列式分析；仅作为指标、token 和训练样本的派生快照，不能替换原始事件事实源。[Parquet 官方介绍](https://parquet.apache.org/docs/overview/)

## trajectory.v1

每条轨迹包含 header、按序 events、完整性 footer。公共元数据包括：`trajectory_id`、`run_id`、`task_id`、`kind=trader|researcher`、split、配置哈希、数据哈希、policy revision、schema version、completed/failed 状态。事件使用递增 sequence、带时区 occurred_at、event_type 与 JSON payload。事件时间不等于模型当时的 decision_time，后者必须写入观察/决策 payload。

金额、净值、仓位和奖励中的精确十进制数保存为字符串；时间用带时区 ISO-8601。禁止 NaN/Infinity。大内容放 blob，以 SHA-256 引用；同样的历史财报和 snapshot 可复用，避免每个事件重复存储。

建议事件类型如下，事件专用 payload 校验将在采集适配器实现；当前通用归档仅检查 envelope、顺序、时间、JSON 和内容完整性。

| 事件 | payload 的关键内容 |
| --- | --- |
| observation | decision_time、可见 record IDs、实际模型输入工件哈希 |
| policy_sample | 模型/checkpoint、tokenizer/render revision、sampling 参数、输入/输出 token IDs、逐 token behavior logprobs、loss mask、finish reason、响应文本工件哈希 |
| decision | 解析后的目标仓位、解析状态、关联 sample sequence |
| session_settled | 原始成交价、股数、费用、现金/持仓、净值、拒单和陈旧估值标记 |
| reward | reward revision、各评分分量、与 Benchmark 指标分开的训练 reward |
| evaluation | 终局报告、goal_success、配置/数据/模型版本 |
| researcher_proposal | 假设、父 checkpoint、代码 diff 工件、训练计划、所访问反馈 IDs |
| tool_call / tool_result | Harness 原始调用 ID、工具参数/结果工件、执行状态 |

JSONL 首行 `record_type=header`，中间每行 `record_type=event`，尾行为 `record_type=footer`，记录 event_count 与此前所有原始字节的 SHA-256。数据库内部保存规范化 JSON envelope 和整体哈希，摘要索引放在同一事务中。该哈希用于意外损坏检测，不是签名或防恶意篡改证明。

## 训练导出与能力边界

SFT 导出模型实际看到的 messages 和示范 response；DPO 导出同一输入下的偏好对；RL 导出确切 token IDs、behavior logprobs、loss masks、奖励和 policy revision。没有保存对应采样概率的普通聊天日志不能冒充有效的 on-policy RL 数据。

`Trajectory.training_reference()` 只接受 completed、trader、train。Researcher 日志不能直接成为 Qwen trader 的训练轨迹；如需蒸馏，必须通过单独的数据构建与审核用例生成新的 train 工件。当前通用 JSONL 导出允许审计各类轨迹，不等于训练导出；SFT/DPO/RL 数据转换尚未实现。

研究者和训练进程不共享 unrestricted 数据库访问权限：完整数据库由存储服务持有，通过受控查询/导出开放 train 与允许的 validation。sealed test 由评估进程独立读取。当前 store 是本地开发适配器，尚未部署权限网关。

实现位置：[领域契约](../../quant_rsi/domain/trajectory.py)、[SQLite/工件适配器](../../quant_rsi/adapters/trajectory_store.py)、[存储配置](../../configs/storage/trajectories.toml)。测试覆盖重启读取、重复写入、冲突拒绝、损坏检测、JSONL 哈希、类型/分区训练限制和 blob 校验。
