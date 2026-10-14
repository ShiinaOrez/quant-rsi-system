# 本地 Qwen trader 试运行

这是 INTC 60 个真实交易日的流程诊断：2024-07-11 收盘决策，2024-07-12 开盘首次成交，2024-10-04 收盘结束。沿用原 TOML 题目的 5% 净收益、10% 最大回撤要求。

当前数据仅包含 Yahoo 原始价历史和 Intel Q1/Q2 2024 官方财报发布文件，不是完整历史披露档案。题目保留 draft，数据 manifest 保留 `benchmark_ready=false`；命令必须显式带 `--pilot`。不能据此宣称完整 Benchmark 已验证或 RSI 已有效。

2026-10-14 本机实际运行完成，60 次模型决策耗时 482.84 秒，轨迹复放与原报告完全一致，28 项测试通过。结果：[原始指标](runs/intc-qwen3-8b-20261014/result.json)、[运行核查](runs/intc-qwen3-8b-20261014/audit.json)、[冻结数据摘要](runs/intc-qwen3-8b-20261014/data-manifest.json)。这些入库文件是审计记录；原始数据和完整轨迹仍在本机 artifacts 中，不应把文档目录的 manifest 当成可执行数据 bundle。

| 策略 | 净收益 | 最大回撤 | 达标 |
| --- | ---: | ---: | --- |
| 本地 Qwen3-8B | -22.20% | 28.23% | 否 |
| 买入持有 | -32.80% | 45.54% | 否 |
| SMA20/60 | -42.35% | 43.13% | 否 |
| 现金 | 0.00% | 0.00% | 否 |

模型在 8 月 1 日收盘仍看 Q1 报告，8 月 2 日收盘看到 Q2 后从 60% 降至 30% 目标仓位；大跌已发生。这是本题时序难点的实际诊断，不足以证明未后训练模型具备交易优势。只评估一次、使用不完整档案，后续正式对比不能依此做统计结论。

首次准备（Python 3.12）：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-market.lock
# 使用空目录；重新下载是新数据 vintage，不保证与原始下载的摘要相同。
.venv/bin/python -m scripts.prepare_intc_pilot --download
```

本机已安装 Ollama 到 `~/Applications/Ollama.app`，模型存在 `~/.ollama/models`。启动和安装：

```sh
OLLAMA_HOST=127.0.0.1:11434 ~/Applications/Ollama.app/Contents/Resources/ollama serve
# 在另一个终端运行；本机模型已下载，无需重复。
~/Applications/Ollama.app/Contents/Resources/ollama pull qwen3:8b
```

本机网络把官方存储 CDN 解析到代理私网地址，`ollama pull` 的重定向检查拒绝了下载。本次通过官方 registry manifest 下载指定 blobs，逐个校验大小与 SHA-256，没有关闭 Ollama 的检查。相同网络问题可用 `.venv/bin/python -m scripts.fetch_ollama_qwen`；模型 tag 可能随上游变化，正式复现以部署记录和实际下载摘要为准。

运行与精确会计复放：

```sh
.venv/bin/python -m scripts.run_local_benchmark --pilot --run-id intc-local-001
.venv/bin/python -m scripts.replay_saved_run artifacts/runs/intc-local-001
.venv/bin/python -m unittest discover -s tests
```

同一 run-id 不允许覆盖。输出在 `artifacts/runs/<run-id>/result.json`、`trajectory.jsonl`；权威轨迹在 `artifacts/runs/trajectories.sqlite3`，完整请求与响应由 SHA-256 工件引用。所有模型权重、行情文件和运行工件都被 Git 忽略，代码与复现说明入库。

模型只收到截止决策时间已发布的最新行情摘要和最近一次财报摘录；完整原始文件保留在数据目录。本次上下文选取策略没有提供全部历史财报与新闻。指标使用截止当日数据，交易在下一交易日开盘执行，5 bps 手续费、5 bps 滑点、整数股、无杠杆和卖空。首次买入后无订单的 buy-and-hold、现金和 SMA20/60 与 trader 使用同一个结算引擎。

8 月 1 日财报发布时间为收盘后，8 月 1 日收盘决策看不到，8 月 2 日收盘才可用。8 月 7 日除息按开盘交易前持仓锁定权益，每股 $0.125；9 月 1 日周末派息，在 9 月 3 日首次交易前记账。当前无现金利息，此时间简化不改变可交易现金；限制记录在 manifest。拆股仍明确拒绝，当前窗口没有拆股。

复现分两层：同一模型硬件/版本可能仍产生不同输出；保存的 60 个动作与数据摘要可确定性复放会计结果。正式训练后模型仍使用相同 TraderPolicy 端口；数据库与 BenchmarkRunner 的替换不依赖 Ollama。

轨迹属于 validation；未采集 token IDs/logprobs，不能直接用于训练。正式数据绑定、完整披露、污染控制、多随机种子与训练闭环仍是后续工作。
