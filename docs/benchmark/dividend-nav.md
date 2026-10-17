# 未付股息与净值

环境 `us-equity-daily-v2.toml` 使用 `single_asset_daily_v2`，终值规则明确为 `mark_to_market_including_dividend_receivables`。已有 20 题暂未迁移到这个环境；正式 R0 前应统一绑定新契约并核对配置哈希。

假设持有 10 股、每股 100 美元，没有现金。每股除息 1 美元后，股价为 99 美元，此时有股票资产 990 美元和应收股息 10 美元，净值仍为 1,000 美元。付款前这 10 美元不能用于下单；付款后转入现金，应收归零，净值不会重复增加。即使除息日卖掉股票，已经形成的付款权益仍然保留。

v2 在区间内和终点都采用 `cash + shares × close + unpaid_dividend_entitlements`。目标股票权重以包含应收的净值计算，买入数量仍受实际现金约束。应收按宣布金额计价，不计利息；付款日发生在非交易日时，编译器须保留真实付款日期，并另记首个后续交易日的现金入账事件。取消股息等尚未实现的公司行动必须明确拒绝。

旧契约 v1 保留原来的现金及股票估值，便于重放已经保存的诊断结果。新契约通过 TOML 的引擎与估值字段共同选择，校验器拒绝将 v2 配上旧的终值口径。领域账户的 `dividend_receivable` 与 `cash` 分开记录；v2 本地模型输入也会分别展示，旧版模型请求保持原有结构。

两个已保存发行人原文的例子：NVDA 的 [FY2024 Q2 公告](https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2024) 声明 2023-09-28 付款，晚于该题 2023-09-26 的终点；[FY2025 Q2 公告](https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2025) 声明 2024-10-03 付款，晚于该题 2024-09-24 的终点。META 另有一个候选例子，当前还缺原始字节，保留未验证状态。

`tests/test_dividend_nav.py` 覆盖终点应收、除息后卖出、终点付款不重复计价、不能花费未付款权益，以及配置契约一致性。2026-10-17 的 INTC 旧版诊断记录经过 60 次动作重放，报告与原记录一致。此检查不构成完整 20 题的数据就绪证明。拆股等未实现的执行行为仍会明确失败。
