# R0 逐题准入

0/20 题已准入；正式 R0 仍阻断。

计数为固定参考索引中截至各题终点的六类表单。已取得列包含正文、实例、封面或未绑定 ZIP，不能当作完整申报数。日期分组不代表已开放模型可见性。

| 题目 | 参考申报 | 已取得某类内容 | 价格窗口 | 最新季报的已取得范围 |
|---|---:|---:|---|---|
| aapl-2023-midyear-60d | 329 | 62 | 通过 | 未纳入来源清单 |
| intc-2024-07-11-60d | 538 | 135 | 通过 | issuer_xbrl_zip_unbound |
| msft-2022-12-30-60d | 376 | 1 | 通过 | 未纳入来源清单 |
| msft-2023-06-30-60d | 381 | 1 | 通过 | 未纳入来源清单 |
| msft-2024-06-28-60d | 398 | 1 | 通过 | 未纳入来源清单 |
| amzn-2022-12-30-60d | 340 | 93 | 通过 | financial_instance_candidate |
| amzn-2023-06-30-60d | 347 | 100 | 通过 | financial_instance_candidate |
| amzn-2024-06-28-60d | 360 | 113 | 通过 | financial_instance_candidate |
| meta-2022-12-30-60d | 150 | 83 | 通过 | financial_instance_candidate |
| meta-2023-06-30-60d | 157 | 90 | 通过 | financial_instance_candidate |
| meta-2024-06-28-60d | 170 | 103 | 通过 | financial_instance_candidate |
| mu-2022-12-30-60d | 472 | 19 | 通过 | financial_instance_candidate |
| mu-2023-06-30-60d | 478 | 19 | 通过 | 未纳入来源清单 |
| mu-2024-06-28-60d | 494 | 21 | 通过 | financial_instance_candidate |
| pypl-2022-12-30-60d | 126 | 8 | 通过 | 未纳入来源清单 |
| pypl-2023-06-30-60d | 137 | 8 | 通过 | 未纳入来源清单 |
| pypl-2024-06-28-60d | 159 | 9 | 通过 | 未纳入来源清单 |
| nvda-2022-12-30-60d | 310 | 31 | 通过 | 未纳入来源清单 |
| nvda-2023-06-30-60d | 316 | 31 | 通过 | 未纳入来源清单 |
| nvda-2024-06-28-60d | 329 | 32 | 通过 | 未纳入来源清单 |

阻断项按题保存在 JSON 的 `blockers`，每份参考申报及来源范围在 `filing_inventory`。当前来源清单缺失不等于磁盘没有该文件。R0 不依赖 Tinker、DeepSeek、Supabase 或训练集。

生成命令及构建/执行协议见 [R0 执行](r0-execution.md)。本次 `run_r0 --execute` 实际返回 `blocked_before_inference`，退出码 2、模型调用 0、正式分数为空。没有启动训练或数据库迁移。
