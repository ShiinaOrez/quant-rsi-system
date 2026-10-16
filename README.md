# Quant RSI System

在模型驱动的量化系统之上，通过递归自我改进（Recursive Self-Improvement，RSI，暂定含义）迭代量化专用后训练模型。

项目目标：可扩展、可执行、可复现的量化 Benchmark；可运行的交易系统；可追溯的模型权重与训练流程；可评估和回滚的 RSI 迭代系统。

当前已建立文献调研、Benchmark v0.1 TOML 契约及 DDD 核心领域实现，新增本地 Ollama/Qwen trader、真实历史数据试运行、分红结算和可复放轨迹。完整历史披露档案与训练闭环尚未就绪，尚未产生本项目训练权重或复现论文收益。

- [DDD 领域模型与实现边界](docs/architecture/domain-model.md)
- [存储端口与可替换 Benchmark](docs/architecture/ports-and-benchmarks.md)：RSI 使用版本化 BenchmarkRunner；存储、工件与导出独立注入。
- [Researcher 接入与轨迹存储选型](docs/architecture/researcher-and-trajectories.md)：DeepSeek Harness + DeepSeek 官方模型接口；SQLite 权威归档、JSONL 导出和 SHA-256 工件存储。
- [首个 trader 模型：Qwen3-8B](configs/models/qwen3-8b.toml)：Tinker LoRA 起点，尚未完成在线接口验证；模型已做通用后训练，并非原始预训练 Base。
- [本地 Qwen 运行与 INTC 60 日试运行](docs/benchmark/local-pilot.md)：真实行情子集，明确保留数据未完整状态。
- [本地部署与量化版本记录](configs/models/qwen3-8b-local.toml)：模型摘要、运行参数与本机硬件。
- [Tinker checkpoint 本地部署](docs/research/tinker-local-deployment.md)：LoRA 导出、合并与 Mac 推理路径。
- [DeepSeek Harness Researcher 范式与复用调查](docs/research/research-agent-paradigms.md)：近期论文、源码接口和受控实验闭环设计。
- [Researcher 六个业务工具与 MCP 接入](docs/architecture/research-tools.md)：带运行时校验的 Schema、事务工作流、成对实验和可执行 demo。

运行领域 demo（人工行情，不是历史回测）：

```sh
python3 -m scripts.demo_domain
```

- [Benchmark 配置契约](docs/benchmark/specification.md)
- [可复用日频环境](benchmarks/environments/us-equity-daily-v1.toml)
- [单标的周期题目](benchmarks/tasks/aapl-2023-midyear-60d.toml)
- [INTC 财报事件诊断题](benchmarks/tasks/intc-2024-07-11-60d.toml)：沿用相同目标和交易约束，[难点与数据计划](docs/benchmark/intc-challenge.md)。难度尚待基线回测验证。

配置校验与测试需要 Python 3.11+，无第三方依赖：

```sh
python3 scripts/validate_benchmark.py benchmarks/tasks/aapl-2023-midyear-60d.toml
python3 -m unittest discover -s tests
```

Researcher 工具与 MCP 测试需先安装可选依赖：`.venv/bin/python -m pip install -e '.[research]'`；未安装时这部分测试跳过。

- [第一轮文献调研与方向判断](docs/research/literature-review.md)
- [实验与系统设计草案](docs/research/research-plan.md)
- [Tinker 后训练与公开 trader 复用调查](docs/research/tinker-trader-reuse.md)
- [结构化论文目录](docs/research/papers.json)
- [BibTeX 引用](docs/research/references.bib)

引用记录锁定实际阅读版本，检索日期为 2026-10-14。阅读范围为摘要及选定的方法、实验章节，并非逐页精读。代码、数据、权重的可下载性及复现能力尚未逐项核验。
