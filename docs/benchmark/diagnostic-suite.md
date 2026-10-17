# NASDAQ 20 题诊断集

套件索引：[`nasdaq-diagnostic-v1.toml`](../../benchmarks/suites/nasdaq-diagnostic-v1.toml)。它包含原有 AAPL、INTC 两题，以及 18 道固定网格题。所有任务仍为 draft；新增题还没有完整数据 manifest，也没有基线成绩。现有 [INTC 试运行](local-pilot.md) 使用的是披露范围受限的数据子集。

## 选择与目标

新增题取 MSFT、AMZN、META、MU、PYPL、NVDA，在 2022-12-30、2023-06-30、2024-06-28 收盘后开始决策，每个组合都保留。目标仍为 60 个交易日内净收益至少 5%、最大回撤不超过 10%，初始空仓、资金 10 万美元。模型每日收盘后决定目标仓位，下一交易日开盘成交；不做空、不加杠杆，佣金和滑点各 5 bps。

| 截止日 | 首个成交日 | 第 60 个交易日 |
| --- | --- | --- |
| 2022-12-30 | 2023-01-03 | 2023-03-29 |
| 2023-06-30 | 2023-07-03 | 2023-09-26 |
| 2024-06-28 | 2024-07-01 | 2024-09-24 |

日期使用 XNAS 日历核对；冬季截止日是 UTC-05:00，夏季是 UTC-04:00。区间内的半日市也计一个交易日，不能简单累加工作日。

| 标的 | 主要检查对象 |
| --- | --- |
| MSFT | 云业务、其他分部、利润与现金流，以及资本支出和会计估计 |
| AMZN | 零售与 AWS 的利润贡献、Rivian 估值变动、资本支出和租赁现金流 |
| META | 广告收入与价格、Reality Labs、重组费用和资本支出 |
| MU | 财季与自然日期、库存减记及其后续成本影响、经营现金流和资本支出 |
| PYPL | TPV 与收入、交易利润、信用风险、non-GAAP 及现金流口径变化 |
| NVDA | 数据中心与其他业务、库存和应收款、预期与已实现收入、拆股后的历史价格 |

这些方向用于发现数据使用和决策中的问题，尚未证明某道题比普通交易窗口更难。财报分析得当也不保证通过收益目标。后续先跑 cash、buy-and-hold、SMA20/60，再讨论题目难度；保留基线轻易通过的题，避免只报告模型失误的窗口。

逐题评估者说明：

| 标的 | 2022 年底 | 2023 年中 | 2024 年中 |
| --- | --- | --- | --- |
| MSFT | [经营利润与净利润](../../benchmarks/assessor/msft-2022-12-30-60d.toml) | [云业务与个人计算](../../benchmarks/assessor/msft-2023-06-30-60d.toml) | [并购与资本投入](../../benchmarks/assessor/msft-2024-06-28-60d.toml) |
| AMZN | [分部利润与自由现金流](../../benchmarks/assessor/amzn-2022-12-30-60d.toml) | [现金流改善](../../benchmarks/assessor/amzn-2023-06-30-60d.toml) | [利润与折旧估计](../../benchmarks/assessor/amzn-2024-06-28-60d.toml) |
| META | [广告价格与投入](../../benchmarks/assessor/meta-2022-12-30-60d.toml) | [重组费用](../../benchmarks/assessor/meta-2023-06-30-60d.toml) | [支出指引与资本返还](../../benchmarks/assessor/meta-2024-06-28-60d.toml) |
| MU | [库存与现金消耗](../../benchmarks/assessor/mu-2022-12-30-60d.toml) | [减记的两种影响](../../benchmarks/assessor/mu-2023-06-30-60d.toml) | [毛利与资金占用](../../benchmarks/assessor/mu-2024-06-28-60d.toml) |
| PYPL | [投资收益与支付经营](../../benchmarks/assessor/pypl-2022-12-30-60d.toml) | [两种利润率](../../benchmarks/assessor/pypl-2023-06-30-60d.toml) | [新旧 non-GAAP](../../benchmarks/assessor/pypl-2024-06-28-60d.toml) |
| NVDA | [库存与采购承诺](../../benchmarks/assessor/nvda-2022-12-30-60d.toml) | [指引与实际收入](../../benchmarks/assessor/nvda-2023-06-30-60d.toml) | [拆股与每股数据](../../benchmarks/assessor/nvda-2024-06-28-60d.toml) |

## 文件与可见性

每道新增题有三份 TOML：

- `benchmarks/tasks/`：环境、截止日、交易周期、目标与数据 manifest。运行器继续按现有任务契约读取。
- `benchmarks/assessor/`：诊断问题、来源、待核对口径和轨迹检查项。只供评估者使用，不能加入交易模型提示词。
- `benchmarks/data-plans/`：该题的截止日、发行人、起始来源和数据缺口。v0.2 计划引用 `us-equity-history-v1.toml` 的共同要求。

数据计划的 `template` 只表示共同构建要求，题目字段补充身份、时间和种子来源；不提供覆盖共享可用性规则的机制。套件校验器检查引用和关键边界，当前数据准备器并不会据此自动下载或编译完整档案。套件本身含评估者信息，也不能直接传给交易模型。

财报链接是核查入口。实际构建数据时仍须收齐可取得的历史披露、版本化三张报表、历史行情与公司行动，并记录覆盖缺口和许可。优先绑定 SEC 当时提交的附件，保留 accession 和公开时间；今天访问 IR 页面得到的 HTML 可能含当前导航或后来改写的内容，不能直接视为当时的完整快照。

只有日期的披露，按发布日期之后的下一交易日开盘才可用。未来文档留在环境私有数据中，随公开时间逐步揭示。历史比较数被重编时，新版本只能从重编公开之后出现，不能覆盖早期观察。

评估者中的财务、时点和执行检查项尚未全部实现为自动评分。它们应对应可审计的来源引用、检索时间、观察与动作轨迹；当前正式成绩仍采用环境定义的收益、回撤等指标。

## 校验

仅检查定义和文件引用，不需要市场数据依赖：

```sh
.venv/bin/python -m scripts.validate_suite benchmarks/suites/nasdaq-diagnostic-v1.toml
```

核对交易日与首尾区间，需要安装项目固定的 `market` 依赖：

```sh
.venv/bin/python -m scripts.validate_suite benchmarks/suites/nasdaq-diagnostic-v1.toml --with-calendar
.venv/bin/python -m unittest discover -s tests
```

校验通过不意味着数据已经物化、模拟器实现了全部环境规则或目标能够达成。绑定完整数据并核对哈希、公司行动、时间可用性和执行规则后，才能逐题进入 ready 状态。

补齐过程与运行规则见[质量迭代协议](quality-iteration.md)、[公开档案采集](archive-materialization.md)、[发行人镜像](issuer-mirrors.md)和[财务事实抽取](financial-extraction.md)。[未付股息估值](dividend-nav.md)已提供 v2 契约，当前题目尚未迁移。数据采集批次不等于正式的三轮题目质量迭代。

## 解释成绩的限制

新增 18 题都属于 validation。这些历史窗口在今天选出，固定网格只能减少按后续收益挑窗口的自由度，不能消除发行人选择偏差、预训练记忆或幸存者偏差。原有 AAPL 的 test 字段也不能证明它已经封存。

同一时点的题受共同市场环境影响，同一发行人的题共享大量历史材料。应报告逐题结果、按时点和发行人分组的结果，并检查与训练数据的文档重叠；不能把 20 题当作 20 个独立样本估算显著性。本轮六只股票集中在科技及相关行业，不代表全市场或多资产能力。
