# Trader 历史查询协议

`OllamaHistoryTrader` 使用 `InformationArchive` 端口，不依赖 SQLite 或文件存储。每次决策按环境的股票和时钟创建 `PointInTimeHistory`，模型只能使用 `search_history`、`read_history`，或提交交易动作。查询不接受覆盖股票、时间或文件路径的参数。

请求使用 `ollama-history-json.v1` JSON 协议，每轮返回工具调用或 action。默认最多 6 次查询，输入最多 12,000 UTF-8 字节，超过预算明确失败。过大的工具页会返回错误，允许模型缩小分页参数；不会静默截断文档。模型采样通过 `capture` 保存，查询及结果通过独立的 `tool_capture` 保存，二者均可接入轨迹组件。此适配器没有采集 token ID 或行为 logprob，不能据此宣称训练数据满足 on-policy 要求。

初始观察包含当前 bar 和文档目录，财报正文经工具分页读取，避免将整份原文重复放入提示。工具描述明确区分字面子串搜索和空查询列目录，并返回剩余调用次数。该说明来自协议本身，不含未来结果或 assessor 信息。

默认允许模型自行决定是否查询。`require_history_evidence=True` 是显式的另一项运行配置：模型必须先搜索，再阅读搜索结果中的非空记录，之后才开放 action。该配置应在正式 R0 之前固定并写入运行说明，不可运行后根据成绩调整。阅读记录只是流程要求，不证明模型理解了证据。

现有 `FrozenPilotMarket.scan` 只暴露 pilot 已有的少量记录，不能补出缺失历史。可用以下命令验证本地推理协议，不产生 benchmark 得分：

```sh
.venv/bin/python -m scripts.check_history_trader --require-evidence --output artifacts/benchmark-quality/history-tool-check.json
```

该命令会保留完整请求、响应、查询和错误；输出路径必须尚不存在。旧 `OllamaTrader` 及其单次调用重放流程保持独立。

现有 pilot runner 可通过 `--history-tools` 启用多轮查询，另加 `--require-history-evidence` 才要求先查后读。运行结果保存查询配置，策略 revision 区分工具配置；采样与查询事件写入同一份轨迹。

```sh
.venv/bin/python -m scripts.run_local_benchmark --pilot --history-tools --require-history-evidence --run-id history-pilot-check
.venv/bin/python -m scripts.replay_saved_run artifacts/runs/history-pilot-check
```

离线重放使用保存的模型响应，重新执行每次查询，逐次比较完整模型请求和工具记录，再核对交易报告；不用网络，也不再次调用模型。改变查询结果会导致重放失败。它仍使用不完整的 pilot 数据；完整档案编译器和 20 题套件 runner 尚未接入，不能把流程验证描述为正式成绩。
