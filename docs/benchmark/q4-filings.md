# 发行人 IR（Q4）SEC 文件 feed

代码：`quant_rsi/adapters/q4_filings.py`（离线计划/核对）、`scripts/q4_sources.py`（plan / fetch / parse），测试 `tests/test_q4_filings.py`。

## 输入

- 已保存的 `GetEdgarFilingList` 全量响应（Store `artifacts/benchmark-quality/issuer-recheck-20261017`，key `meta-financials-all-sec-feed`、`amzn-sec-filings-all-sec-feed`）。脚本不会再请求 feed。
- 参考索引 `artifacts/benchmark-quality/public-index/import-2026-09-30-v2/{META,AMZN}.jsonl`。

## 规则

- 发行人固定：META CIK 1326801、AMZN 1018724。行必须 `StockExchange=CIK` 且 `StockSymbol` 等于 10 位 CIK，否则排除并记下 row_index（两家 feed 里各有一批 `0001364742` 的 SC 13G/A 行）。
- 表单只取 10-K、10-K/A、10-Q、10-Q/A、8-K、8-K/A；`FilingDate` 必须是合法日历日期，格式为 `MM/DD/YYYY 00:00:00`，且不晚于 through（2024-10-04）。
- 每行保留原始字段 `raw`、源 blob sha256、`row_index`。`available_at` 恒为空：`ReceivedDate`/`CreateDate` 不算已验证的公开时间。
- DocumentType 角色：`ORIG` 是 SEC 原件，但在观察到的 feed 中 URL 全为空 → 缺口；`CONVPDF`/`CONVTEXT`/`XLS` 是 Q4 派生物；`XBRL` ZIP 不等于全部附件；`XBRL_HTML` 是 Q4 自渲染页面（实测，见下），不是 iXBRL 原件；未知类型保留为 `unknown_type`，不可抓取。
- URL 白名单：同源 detail 路径（仅允许 `FilingId=<本行数字>`）；`https://d18rn0p25nwr6d.cloudfront.net/CIK-<本发行人10位CIK>/<UUID>.<按类型限定的扩展名>`。拒绝 http（不偷偷升级）、外域、userinfo、query/fragment、路径异常、CIK 错配。不跟随重定向。
- accession：`FilingId`/`EdgarFilingId` 绝不转换。detail 页面若含 `sec.gov/Archives` 链接，且 CIK/表单/日期与参考行一致，才可绑定（`accession_bound`）。否则只在 (issuer, form, filingDate) 两侧都唯一时给 `accession_candidate` + `match_basis`，`accession_verified=false`；多对多记为 `ambiguous_same_day`，单侧记为 `q4_only` / `reference_only`。
- 抓取：项目 UA，串行 ≥1 秒，45 秒超时，不重试，单文件 64 MiB（超限记为失败，只存部分字节的 hash，不入 catalog），默认每次 ≤12 请求，可显式设为 1–250；总响应预算 64 MiB，超限检测最多多读 1 字节。360 秒是请求准入时间预算，45 秒是每次 I/O 超时，均不宣称为总墙钟硬上限。403/429 后停止联网。抓取前重新检查 URL；缓存只有 key、URL、元数据一致且 blob 哈希正确才复用。Content-Length 不符或响应未读完不能进入成功 catalog。
- 解析：ZIP 走 `xbrl_instance.extract_any_zip`，HTML 若含 iXBRL 命名空间才走 `inline_xbrl.extract_document`。只在有唯一候选 accession 时调用（解析器要求 accession），结果里标 `accession_verified=false`；不传 available_at。

## 用法

```
.venv/bin/python scripts/q4_sources.py plan    # 离线
.venv/bin/python scripts/q4_sources.py fetch   # 联网，抽样
.venv/bin/python scripts/q4_sources.py parse   # 离线
```

默认输出到 `artifacts/benchmark-quality/claude/batch-11/q4/`，原字节在 `.../batch-11/q4-store/`。

## 2026-10-17 实测结果

Meta 170 行、Amazon 360 行与固定参考索引的表单/日期分组无单侧缺失；其中 7 行同日歧义保留。两份真实 detail 页面没有 SEC archive 链接，所以其余匹配也只算 accession 候选。

首批下载 6 份 XBRL ZIP、2 份 Q4 派生 HTML、2 份 detail，共 8.4 MB。6 份 ZIP 的 CIK、表单、报告期和主文档名均与候选一致。父进程修复 unit 默认命名空间后重跑，数值错误从 700 条降至 0。原文、修复前后结果均保留，不补写公开时间。详见 `artifacts/benchmark-quality/claude/batch-11/handoff.md` 与 `q4-parent-unitfix-v1/`。

批量补取可使用：

```sh
.venv/bin/python scripts/q4_sources.py fetch --selection all-xbrl --max-requests 60
```

此选择只处理已保存计划内可抓取的 XBRL ZIP，逐份沿用身份、URL、预算检查；`samples` 仍为默认。抓取结果和错误单独保存，已成功文件可续用。当前两个计划共有 199 个可抓取 ZIP；这不包括被拒绝的旧 CDN 链接，也不表示完整提交附件已齐。

父进程扩量已完成：`artifacts/benchmark-quality/q4-expanded-v1/coverage-summary-v4.json` 记录 META 86 份 ZIP（7,918,676 字节、29,140 条事实）、AMZN 113 份（13,672,111 字节、54,075 条事实）。总计 83,215 条事实，其中数值正常 73,235 条、nil 215 条、数值错误 0。原字节和下载日志完整保存；本轮增量共 194 次请求：193 次成功，一次 TLS 提前断开在后续续跑批次重取成功；另复用原先 6 份 ZIP。“份数”按不同 FilingId 计，相同字节由 CAS 去重，不能据此增加独立证据。

内容佐证识别旧 DEI 命名空间（`http://xbrl.us/dei/2008-03-31`、`2009-01-31`）及 SEC 年份、季度版本。修订表单必须精确匹配，不能去掉 `/A`。缺少有效 DEI 时记为不可核对；有值但不同才算冲突。199 份的 CIK、表单、报告期均一致；148 份 `_htm.xml` 文件名可与参考主文档核对，51 份传统 instance 不适用该命名推导，结果为 null。最初核对器产生的误报和后续修正结果均保留，最终看 v4。所有 accession 仍为候选，公开时间为空，不把这些结果计为正式题目准入。
