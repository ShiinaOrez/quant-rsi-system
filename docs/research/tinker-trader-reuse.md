# 公开 trader 与 Tinker 后训练复用调查

核验日期：2026-10-14。结论：存在公开、原生调用 Tinker 做权重后训练的预测市场 trader 示例；本轮未找到可以直接满足本项目单股票、60 日、时点财报与持仓账本契约的完整系统。推荐组合复用，而非等待 Trading-R1 发布。

检查范围：官方文档、仓库文件树、选定核心源码及权重页面。未安装这些项目、未调用付费 API、未运行训练或复现收益。固定源码版本见 [复用目录](reuse-candidates.toml)；链接中的 main 会变化，应优先使用目录中的 commit。

## 候选比较

| 候选 | 已核验工件 | Tinker 接入判断 | 建议 |
| --- | --- | --- | --- |
| [prime-v-tinker-trader](https://github.com/Sohailm25/prime-v-tinker-trader) | Tinker SFT/PPO 源码、预测市场 JSON | 原生调用；SDK 兼容性仍待运行 | 学习最小权重更新闭环 |
| [Tinker Cookbook](https://github.com/thinking-machines-lab/tinker-cookbook) | Env/StepResult、SFT、RL、DPO、forecasting 代码 | 官方训练底座 | 优先复用 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 分析/决策 graph、可替换 LLM client、决策评估 | 需训练适配层 | 固定模型基线与示范生成 |
| [FinRL-DeepSeek](https://github.com/benstaf/FinRL_DeepSeek) | 数值交易环境、PyTorch MLP PPO、数据加载入口 | 不是 LLM 权重更新接口 | 数值策略基线参考 |
| [Fin-R1](https://github.com/SUFE-AIFLM-Lab/Fin-R1) | README/报告；HF 模型权重文件列表 | 未确认可直接导入 Tinker | 金融推理对照或 teacher |
| [Trading-R1](https://github.com/TauricResearch/Trading-R1) | 官方仓库当前仅 README 待发布 | 暂无官方实现可接 | 方法参考 |
| [Feed，原 Babylon](https://github.com/feedforagents/feed) | 文档与 Git 文件树的 Tinker 配置/runbook | 完整实现未深入核验 | 暂不作为股票系统底座 |

## 直接相关的原生示例

`prime-v-tinker-trader/tinker/train_sft.py` 创建 LoRA training client；`train_ppo_cot.py` 从 SFT checkpoint 继续，生成响应、计算奖励、调用 `forward_backward_custom` 并保存 checkpoint。它确实更新 LLM 权重，包含本地预测市场数据。README 声明 MIT，但本次文件树中未找到独立 LICENSE；实际引入前需补充许可核验。

其动作是 BUY_YES/BUY_NO/HOLD；PPO 奖励使用市场结算 outcome，而非逐日股票净值。HOLD shaping 用事后可获利动作评价错失机会，不宜复制为股票风险管理奖励。未发现仓库内可下载权重文件；脚本会生成 Tinker checkpoint，不等于已有公共权重发布。模型配置使用旧的 Qwen3-4B-Instruct-2507，需迁移到当前可用模型并核对 renderer/API。适合借鉴训练接线，不适合作为已证实盈利的 trader。[源码](https://github.com/Sohailm25/prime-v-tinker-trader/blob/main/tinker/train_ppo_cot.py)

## 更适合本项目的组合

Tinker 官方 Cookbook 的 `rl/types.py` 提供 Env、StepResult、EnvGroupBuilder、RLDataset；官方 forecasting recipe 包含时间切分和 Brier reward，但它预测二元事件概率，不模拟股票成交。复用训练与数据组织结构，另外实现我们的多步交易 Env。[官方 Cookbook](https://tinker-docs.thinkingmachines.ai/cookbook/quickstart/)、[forecasting](https://tinker-docs.thinkingmachines.ai/cookbook/recipes/forecasting/)

TradingAgents 当前 `backtest.py` 明确限定为独立决策评估，不维护跨日持仓、成交价格及现金账本。Yahoo fundamentals 已有历史时点屏蔽，不能把该项目简单归类为“完全无防泄漏”；仍需替换成冻结的本地时点数据。其 graph、分析工具和可配置客户端可以复用。代码许可为 Apache-2.0；不自带本项目所需的金融后训练权重或训练循环。[backtest 源码](https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/backtest.py)

FinRL-DeepSeek 的 `train_ppo_llm.py` 定义 MLPActorCritic，并加载带 DeepSeek sentiment 的数据。训练对象是数值策略网络；“LLM-infused”不能理解成对 DeepSeek 做金融后训练。代码 MIT，可借用环境或作为传统 PPO 对照，动作、撮合和风险规则仍须与我们的契约对齐。[源码](https://github.com/benstaf/FinRL_DeepSeek/blob/main/train_ppo_llm.py)

Fin-R1 的 [Hugging Face 文件列表](https://huggingface.co/SUFE-AIFLM-Lab/Fin-R1/tree/main) 包含四个 safetensors 权重分片；本次 GitHub 版本主要为说明、图像和报告，未发现完整训练脚本。权重可见不等于可直接在 Tinker 继续训练；模型/数据许可与导入路径还需核验。[项目说明](https://github.com/SUFE-AIFLM-Lab/Fin-R1)

Feed 是社会模拟预测市场游戏，原 Babylon 地址重定向至此。文件树可见 `packages/training/deploy/TINKER_RUNBOOK.md` 和 Python Tinker 配置，尚未完成核心代码、许可和公共权重核验；工程范围明显大于单标的历史回放，不优先引入。[项目](https://github.com/feedforagents/feed)

## 接入边界

1. **训练模型**：从 Tinker 支持列表中的基座开始，例如当前文档列出的 `Qwen/Qwen3-8B`。能本地加载某个 HF 模型不等于它是 Tinker 支持基座。本轮未确认任意外部完整权重的导入流程，不假设 Fin-R1 可以直接接续训练。[官方模型列表](https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/)
2. **SFT**：用冻结信息快照生成合法目标仓位和可核验证据，转为 Cookbook 数据。成功交易不自动意味着推理正确；避免事后最优策略伪装成可在线获得的示范。
3. **RL**：TOML → 本地交易 Env → 多日 rollout → 净值/风险反馈 → Tinker sampling 与训练。按相同题目复制独立账户生成 group；不能让样本共用持仓状态。保留 token、logprob、有效动作与成交日志。
4. **奖励**：Benchmark 的目标和评分保持冻结。训练 reward 单独版本化，可先用逐日净值 log-return 与预先定义的风险约束；目标达标仅作为报告指标或终局信号。不能把训练 shaping 改进冒充真实收益改进。
5. **部署**：官方可导出 LoRA adapter，再合并或转换为 PEFT 服务；训练 rollout 优先使用原生 SamplingClient。OpenAI-compatible 推理可供内部试验，但需核对工具调用和 chat renderer，不能只替换 base_url 就宣布 LangChain graph 完全兼容。[checkpoint](https://tinker-docs.thinkingmachines.ai/tinker/howto/checkpoints/)、[推理接口](https://tinker-docs.thinkingmachines.ai/tinker/compatible-apis/openai/)

## 建议的下一步实现

采用“独立的 TOML 交易环境 + 单个可训练 trader + Tinker Cookbook”。TradingAgents 先以冻结分析器/teacher 身份作为对照，不同时训练全部 agent；先完成 INTC 题目的数据和账本，再跑原始基座与固定分析框架。随后用独立训练窗口做小规模 SFT/RL，INTC 诊断题保持验证用途。单题本身不足以构成后训练数据或说明泛化提升。

下一步适配探针应验证：当前可用模型、renderer、结构化仓位输出、多步 Env、token/logprob 记录、checkpoint 导出与推理。通过后再把 API 接入视为已验证，而不是根据 README 声称一键可用。
