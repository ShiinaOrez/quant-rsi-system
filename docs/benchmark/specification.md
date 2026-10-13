# Benchmark 配置契约 v0.1

Benchmark 由 TOML 描述文件定义，文字只解释字段语义，不能覆盖配置。v0.1 聚焦单标的、日频、无杠杆交易。TOML 可以直接表达类型、数组和分组，Python 3.11+ 自带解析器，无需额外依赖。后续如增加 YAML，应解析到同一契约，避免两套语义。

## 环境与题目

- `benchmarks/environments/*.toml`：市场、信息访问、指标、动作、执行、估值和评分规则。可被多个题目引用。
- `benchmarks/tasks/*.toml`：标的、历史起点、周期、资金、目标及数据快照。

示例：[环境](../../benchmarks/environments/us-equity-daily-v1.toml)、[题目](../../benchmarks/tasks/aapl-2023-midyear-60d.toml)。费用与目标是初始实验参数，并非实际券商报价或已验证的合理目标。

## 时间和信息语义

`as_of` 是带时区的初始决策时间。仅能读取 `available_at <= decision_time` 的记录。`event_time` 是经济事件或统计期的时间，不能替代发布时间。例如 2023 年 6 月结束的报表不能因统计期已结束就提前可见。

`financial_revision = as_published` 保留当时公开版本；更正报告只能从更正发布时间开始访问。三大财务报表和原始 filings 共同构成基本面信息；历史日线 OHLCV 构成第一版的历史交易信息。全部已公开历史可查询，不要求将其一次性放入模型上下文。

模型在 `as_of` 作出首个目标仓位，下一交易日开盘执行。随后每个交易日收盘提供新观察和决策机会，订单在下一交易日开盘执行。第 60 个交易日收盘结束 episode，不执行任何超出周期的订单。

`horizon_sessions` 按交易日历计算，不是自然日。示例的纽约收盘时间使用显式 UTC offset；执行引擎仍须根据 `timezone` 和 `calendar` 校验它确实对应合法交易日收盘，不能把周末或错误夏令时 offset 当作合法起点。

## 指标定义

所有指标只使用决策时点之前可见的日线。`split_adjusted_as_of` 仅使用截至当前决策时点已生效的拆股调整历史价格，不使用未来拆股或未来分红进行回溯调整。

- SMA：最近 N 个交易日收盘价的算术平均。
- RSI：Wilder 平滑；初始平均涨跌幅为 N 个价格差的算术平均，随后递推。平均跌幅为零且涨幅正时为 100，两者均为零时为 50。
- volatility：最近 N 个 close-to-close 简单收益的样本标准差，`ddof = 1`，不年化；需要 N+1 个收盘价格。
- 历史不足时输出 null，不补未来值。

## 动作、成交和净值

`target_weight` 是标的目标权重，现金占剩余部分。示例限制为 [0, 1]，不做空、不借款。下一开盘用原始开盘价、成交前净值与目标权重计算目标整数股数，再向下取整。买入量进一步下调到包含费用后现金可支付的最大整数股数；卖出不得超过持仓。

买入成交价 = open × (1 + slippage_bps / 10000)，卖出成交价 = open × (1 − slippage_bps / 10000)。手续费 = 成交绝对金额 × commission_bps / 10000。没有开盘价或不可交易时拒单，不自动顺延。此规则不模拟盘口、成交量冲击或真实券商服务。

拆股生效时调整持股数；分红以除息日前持有量确定资格，在支付日计入现金。企业行动账本必须防止调整价格与现金分红重复计算。出现 v0.1 无法处理的企业行动时，执行引擎应中止并标记数据/能力缺口。

每日净值为现金加持股数乘原始收盘价；收盘缺失时用最近已知价并标注陈旧估值。期末按市值估值，不强制平仓，因此未扣假设的期末退出费用。若以后加入强制平仓，应使用新环境版本。

## 目标和评分

初始净值 V0 为初始现金，示例初始持仓为零。所有净值包含已收取的费用和已到账分红。

```text
net_return = V_terminal / V0 - 1
max_drawdown = max_t(1 - V_t / max_{s<=t}(V_s))
goal_success = (net_return >= net_return_gte)
               AND (max_drawdown <= max_drawdown_lte)
```

回撤使用包括 V0 的每日收盘净值序列，不代表日内最大回撤。`turnover` 定义为每笔成交绝对金额 / 成交前净值的累计值；`trade_count` 是非零成交订单数。`excess_return_vs_buy_hold` 为策略净收益减去同期买入持有净收益，以百分点表示，不是相对收益比值。

所有基线使用相同资金、时间、成交规则和费用。现金基线不交易；买入持有在初始决策时给出权重 1 并保持；均线基线在 SMA20 > SMA60 时给出 1，否则给出 0，指标不足时为 0。只允许 episode 开始后交易。

目标只在期末判断，不因中途短暂达标就结束。目标成功与连续指标一起报告。目标、费用与规则必须在查看测试结果前固定。

## 数据绑定与复现状态

`draft` 允许 `UNBOUND`，用于讨论题目。`ready` 必须给出相对题目文件的本地 manifest 路径和该文件真实 SHA-256。manifest 应列出数据文件、SHA-256、来源、许可、抓取版本、可用时间定义、覆盖范围和交易日历来源。

当前校验器检查配置契约、字段类型、未知字段、数值边界、路径和 ready 状态下的 manifest 哈希。它不核验 manifest 中的每个数据文件，不校验行情完整性，也不证明历史信息无泄漏。执行引擎尚未实现；`ready` 只表示配置完成绑定。

运行配置校验（Python 3.11+）：

```sh
python3 scripts/validate_benchmark.py benchmarks/tasks/aapl-2023-midyear-60d.toml
python3 scripts/validate_benchmark.py --require-ready benchmarks/tasks/aapl-2023-midyear-60d.toml
python3 -m unittest discover -s tests
```

第二条命令应拒绝当前草案，防止把未绑定数据的示例当作可执行 Benchmark。以后训练/验证/测试题目必须绑定独立访问权限；`split` 标签本身不提供访问隔离。
