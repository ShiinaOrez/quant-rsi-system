# 第一轮文献调研

检索与阅读日期：2026-10-14。RSI 暂指 Recursive Self-Improvement，不指技术指标 Relative Strength Index。

## 方向判断

本项目最值得验证的假设是：**可执行的量化研究反馈能否转化成训练数据，使后训练后的模型在新的市场窗口中，比固定模型更有效地提出策略或做出决策？** 这是本项目的研究假设，不是下面论文已经证明的结论。

需要区分三种改进：提示词和记忆更新；因子、代码与预测模型的研究迭代；基础语言模型权重的后训练。前两种可以生成第三种的训练样本，但不能凭回测收益上升就宣称基础模型能力提升。金融 QA 正确率也不能替代扣费后的交易表现。

## 检索方法与边界

通过 arXiv 搜索金融 RL 环境、金融 Benchmark、金融 LLM 后训练、自动因子研究、self-training 和 self-play；主要关键词为 `quantitative trading reinforcement learning LLM`、`Fin-R1`、`Trading-R1`、`Qlib`、`RD-Agent`、`AlphaAgent`、`FinBen`、`Reinforced Self-Training`、`Self-Play Fine-Tuning`、`Direct Preference Optimization`。

从检索结果筛出 11 篇核心参考，阅读其摘要及选定的方法/实验章节。引用锁定版本，未声称覆盖所有截至检索日的最新工作。下文“借鉴”与“限制”包含本项目的判断；论文收益为作者报告，未自行复现。官方项目链接只是后续核验入口，不代表数据、权重或完整训练代码已经核验可用。未在仓库复制论文全文。

## 核心论文

| 引用键 | 论文与阅读版本 | 作用 | 优先级 |
| --- | --- | --- | --- |
| qlib | [Qlib, 2009.11189v1](https://arxiv.org/abs/2009.11189v1) | 量化系统基础设施 | P0 |
| finrl_meta | [FinRL-Meta, 2211.03107v1](https://arxiv.org/abs/2211.03107v1) | 可扩展 RL 市场环境 | P1 |
| finben | [FinBen, 2402.12659v1](https://arxiv.org/abs/2402.12659v1) | 金融能力评测 | P1 |
| trading_r1 | [Trading-R1, 2509.11420v1](https://arxiv.org/abs/2509.11420v1) | 交易推理后训练 | P0 |
| fin_r1 | [Fin-R1, 2503.16252v1](https://arxiv.org/abs/2503.16252v1) | 金融推理 SFT/RL | P1 |
| rd_agent_quant | [R&D-Agent-Quant, 2505.15155v1](https://arxiv.org/abs/2505.15155v1) | 因子与模型联合迭代 | P0 |
| alphaagent | [AlphaAgent, 2502.16789v1](https://arxiv.org/abs/2502.16789v1) | 受约束的因子搜索 | P0 |
| fincon | [FinCon, 2407.06567v1](https://arxiv.org/abs/2407.06567v1) | 提示词与投资信念更新 | P1 |
| rest | [ReST, 2308.08998v1](https://arxiv.org/abs/2308.08998v1) | 生成—筛选—训练闭环 | P0 |
| spin | [SPIN, 2401.01335v3](https://arxiv.org/abs/2401.01335v3) | 跨轮次权重改进 | P1 |
| dpo | [DPO, 2305.18290v1](https://arxiv.org/abs/2305.18290v1) | 离线偏好训练基线 | P0 |

### Qlib：系统底座

阅读 §3 的模块化工作流、数据基础设施与表达式引擎。其价值是连接数据、特征、预测和组合评估，适合作为因子研究的起点。借鉴：让固定模型、生成因子和后训练模型共享同一评估接口。限制：平台本身不能保证无泄漏或真实成交。[全文 §3](https://arxiv.org/html/2009.11189v1#S3)。官方入口：[microsoft/qlib](https://github.com/microsoft/qlib)。

### FinRL-Meta：环境与数据契约

阅读 §3–4 的数据/环境/agent 分层、训练—验证—测试—交易流程。论文明确讨论低信噪比、幸存者偏差及反复调参导致的回测过拟合。借鉴：统一动作、成本与收益计算；将 paper trading 接入同一策略接口。限制：历史环境有 sim-to-real 差距，离线指标不能代替成交验证。[全文](https://arxiv.org/html/2211.03107v1)。官方入口：[FinRL-Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta)。

### FinBen：辅助能力层

阅读 §2 的任务、数据与指标表；包含数值 QA、文本分析、预测及交易任务。借鉴：用金融计算与信息抽取评测检查后训练是否损害基础能力。限制：不同数据集有不同许可；静态任务进步不能单独支持投资收益结论。[全文 §2](https://arxiv.org/html/2402.12659v1#S2)。

### Trading-R1：最直接的后训练参考

阅读 §3–4 及失败案例：分阶段 SFT/RFT，GRPO 结合结构、证据与决策奖励；训练样本约 100k，输入上下文很长。报告使用 8×H100 做 SFT、8×H200 做 RL，原配方有明显算力门槛。借鉴：先训练可执行结构与证据约束，再优化决策。复现疑点：§3 数据覆盖 2024-01 至 2025-05；§4.3 测试为被留出的 2024-06 至 08，不能据此确认严格向未来泛化。附录记录了格式奖励造成空洞推理的问题。必须另查时间切分、成本及标签生成细节。[方法与评估](https://arxiv.org/html/2509.11420v1#S3)。官方入口：[Trading-R1](https://github.com/TauricResearch/Trading-R1)。

### Fin-R1：可核验金融推理

阅读 §2 的蒸馏、SFT 与 GRPO；奖励包含格式检查和 LLM 判定答案正确性，评估含 FinQA/ConvFinQA。借鉴：建立金融数值推理训练与辅助评测。限制：QA 的答案正确性和交易的随机回报性质不同；LLM judge 的一致性需要验证。[全文 §2](https://arxiv.org/html/2503.16252v1#S2)。官方入口：[Fin-R1](https://github.com/SUFE-AIFLM-Lab/Fin-R1)。

### R&D-Agent-Quant：RSI 的外循环参考

阅读 §2 和消融：规定数据/输出/执行接口，历史实验反馈指导假设，代码执行后回测，调度因子或模型优化。借鉴：结构化保留假设、实现、错误和收益反馈，作为后训练候选轨迹。限制：优化被生成的因子/预测模型，并不等于训练研究者 LLM；反复搜索会增加验证集过拟合。[全文 §2](https://arxiv.org/html/2505.15155v1#S2)。官方入口：[RD-Agent](https://github.com/microsoft/RD-Agent)。

### AlphaAgent：约束比盲目增加搜索更重要

阅读 §3 的表达式算子和 AST；用复杂度、假设一致性、与既有因子的区别约束搜索。借鉴：因子 DSL、结构去重及复杂度预算可作为确定性筛选器。限制：结构新颖不代表经济独立或未来有效；语义评分仍可能依赖 LLM。[全文 §3](https://arxiv.org/html/2502.16789v1#S3)。

### FinCon：记忆改进的对照组

阅读训练/测试算法：风险和损益触发反思，跨 episode 比较轨迹并更新投资信念提示词；测试期停用跨 episode 更新。借鉴：冻结基础模型的反思基线。限制：这里的 verbal reinforcement 更新的是提示词，不应记作神经网络权重后训练。[全文](https://arxiv.org/html/2407.06567v1)。

### ReST：首个闭环应尽量可检查

阅读 Grow/Improve：先生成多个候选，评分筛选后做离线训练，重复使用样本并再次生成。借鉴：将可执行研究轨迹、验证反馈转化为过滤后的训练集。限制：原实验是机器翻译；金融中筛选器噪声、多次搜索和市场漂移需要另行检验。[全文](https://arxiv.org/html/2308.08998v1)。

### SPIN：迭代仍依赖外部锚点

阅读 §4：旧模型生成响应，新模型学习区分生成响应与已有高质量 SFT 数据，再更新旧模型。借鉴：保留跨轮次 checkpoint 和固定示范锚点。限制：并不是无外部信号地不断自证正确；模仿示范不能保证新的市场表现。[全文 §4](https://arxiv.org/html/2401.01335v3#S4)。官方入口：[SPIN](https://github.com/uclaml/SPIN)。

### DPO：易比较的后训练基线

阅读 §3–4：用同一输入下的偏好对直接训练策略，无需单独训练显式奖励模型。借鉴：比较同样市场信息与同等研究预算下的成功/失败候选，构建离线偏好对。限制：偏好标签和覆盖范围决定效果；一次盈利不能作为可靠的 chosen 标签。[全文 §4](https://arxiv.org/html/2305.18290v1#S4)。

## 后续精读与复现问题

优先核验 Trading-R1 的标签/切分与奖励、RD-Agent-Quant 的搜索反馈访问边界、Qlib 的数据和成交假设。逐项记录代码 commit、数据许可、权重地址、训练配置与硬件要求，避免把论文中的“将发布”当作已经可复现。

第二轮应补充金融回测多重检验、LLM 历史知识污染、非平稳市场在线适应的专门文献。本轮不据有限论文做收益承诺，也不把整个方向的长期有效性视为已证明。
