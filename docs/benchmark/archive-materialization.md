# 公开历史档案物化（nasdaq-diagnostic-v1）

工具：`scripts/archive_sources.py`。目标：为套件里固定的 20 道题（AAPL、INTC、MSFT、AMZN、META、MU、PYPL、NVDA，截止日落在 2022/2023/2024）补齐 `benchmarks/data-plans/us-equity-history-v1.toml` 要求的历史档案。题目、评分目标和数据计划都不修改。这个工具只采集数据、报告缺口，**从不把任何题目标记为 ready**（`coverage.json` 中 `benchmark_ready=false`，每题 `pit_ready=false`）。

## 存储与续跑

- `blobs/<sha[:2]>/<sha256>`：按内容寻址的原始字节，写入时原子替换。读取时会重新校验 sha256；校验不过就把文件移到 `quarantine/`，下次重新抓取。
- `logs/fetch-log.jsonl`：只追加。每次逻辑获取都记一条（status、attempts、error、bytes、sha256）。403/超时/重试耗尽都留在这里，错误页的 body 也会存成 blob 作为证据。
- `catalog.jsonl`：只追加，记录成功的版本。`--refresh` 会追加一个新版本，旧版本不覆盖（对应 `later_amendments = append_new_version_never_overwrite`）。
- 续跑方式：`fetch` 跳过 catalog 里已经校验通过的条目。403/404 失败默认不重试，但仍会列入 coverage 的 `failed`；加 `--retry-failed` 才会重试。

## HTTP 策略

- 串行请求，按主机限速：SEC 主机最小间隔 0.25 s（当前实现只串行使用 www/data 两个 SEC 主机，合计不超过 8 req/s；规则见 [SEC 官方访问说明](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)），其他主机 1 s。
- 遇到 429/5xx/超时会指数退避重试，并遵守 `Retry-After`。同一主机连续 3 次 403 就熔断：本次运行不再请求该主机，后续条目在日志中记为 `host_blocked_this_run`。
- User-Agent：默认是 `quant-rsi-system-archive/0.1 (+https://github.com/ShiinaOrez/quant-rsi-system)`，可以用 `--user-agent` 或环境变量 `ARCHIVE_SEC_USER_AGENT` 覆盖。不内置任何个人邮箱。

## SEC 路径（申报文件、财报、附件）

1. `sec-index`：从 `company_tickers.json` 解析 CIK，再与内置的 CIK 表交叉核对，不一致就报错。随后抓 `submissions/CIK##########.json`，以及 `filings.files[]` 里的全部历史分片，然后合并、按 accession 去重。一致性检查包括：分片缺失、`filingCount` 不符、日期越界、同一 accession 字段冲突、accession 格式。注意 META 的旧 ticker 是 FB，CIK 不变，`company_tickers` 只反映当前 ticker。
2. `plan`：plan 中的 6 种表单（10-K、10-K/A、10-Q、10-Q/A、8-K、8-K/A）每份申报生成一条工作项，目标是完整的 as-filed 提交文件 `Archives/edgar/data/<cik>/<acc>/<acc>.txt`。这个文件里有 SGML 头、主文档和**全部附件**（EX-99.1 新闻稿、EX-31/32、XBRL 等），一次请求就拿到原始档案。`10-K405`、`10-KT` 等历史变体会计数报告（`out_of_plan_form_variants`），不静默丢弃。`estimated_bytes` 取自 submissions 的 `size`，用来估算体量：实际体量取决于附件，下载前应检查本地空间。
3. `fetch --limit N`：分批执行。
4. `coverage`：从 SGML 头解析 `<ACCEPTANCE-DATETIME>`（美东时间），得到 `accepted_at`，并与 submissions 中存在的 `acceptanceDateTime` 核对；旧分片缺少这个交叉核对字段时，仍以完整原始提交头为受理时间证据（该字段带 `Z` 后缀，两种读法都接受，但必须和头部一致）。还要核对 PUBLIC DOCUMENT COUNT、实际文档起止标签数和提交结束标签，缺附件或截断不能算完整。缺 `accepted_at` 或核对不一致的申报不计入 `accepted_at_verified`，所在 symbol 的 `complete=false`。这里的 complete 只针对该采集器检查的档案字节与受理元数据，不表示可见时间已核实。INTC 10-Q `0000050863-26-000157` 的受理值为 `20260723184423`，FILED AS OF DATE 是 20260724；不能因此把文件视为 23 日晚间已公开。[SEC 说明](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/determine-status-my-filing)明确存在次日才发布的情形。须另行确定 `available_at` 的依据，不能直接复制 `accepted_at`，也不能使用 filing date 零点。
5. `companyfacts` 只是最新聚合，不能替代原始财报版本，本工具不把它当数据源。三表必须从各期原始申报（XBRL 实例或 HTML）按 `filing_accession` 抽取，这一步还没实现。

## 行情 / 拆股 / 分红路径

- 来源：Yahoo `v8/finance/chart/<SYM>?period1=0&interval=1d&events=div|split`，即 yfinance 背后的接口。实测这个接口的 OHLC 和 volume（包括 yfinance `auto_adjust=False` 拿到的数据）**都按事后拆股做了回溯调整**，只有 `adjclose` 额外做了分红调整。NVDA 2024-06-07 返回 close=120.888、volume=412,386,000，而当日实际成交价约 1208.88。
- 恢复交易日候选原始值（`reconstruct_raw`，`normalization_verified=false`）：`raw_price = price × ∏(该交易日之后生效的拆股比例)`，`raw_volume = volume ÷ 同一因子`。候选成交量不是整数就标记并保留未取整值，不能用四舍五入消除差异。分红金额同样按拆股还原。抽查结果：AAPL 2020-08-28 为 499.23，AMZN 2022-06-03 为 2447.00，NVDA 2021-07-19 为 751.19，NVDA 2024-06-07 为 1208.88。浮点差异（例如 2446.999969）须对照原始价格及对应时期的最小报价单位核实；尚未实施通用的价格取整规则。
- 口径限制：这份数据是**当前下载**，不是历史当时的 vintage。成交量是合并成交量还是上市交易所成交量，尚未核实。原始数据没有许可审查。
- 分红日期：Yahoo 只有除息日。api.nasdaq.com 的 dividends 接口用 curl 加浏览器 UA 能拿到 declaration/record/payment，且金额未经拆股调整（NVDA 2024-03-05 为 $0.04），但它是第三方聚合，只能作交叉核对。工具使用项目 UA 访问时会超时，这一点已如实记录；工具不伪装浏览器 UA。官方 pay date 要从发行人 8-K/EX-99.1 中绑定。
- 日历：还需要用 XNAS（exchange_calendars）审计行情的交易日。另外，盘中抓取的当日 bar 会被标记为排除。

## 命令

```bash
R=artifacts/data/public-archive-v1   # 任意空目录
.venv/bin/python scripts/archive_sources.py targets   --root $R
.venv/bin/python scripts/archive_sources.py probe     --root $R --timeout 20 --retries 1
.venv/bin/python scripts/archive_sources.py sec-index --root $R
.venv/bin/python scripts/archive_sources.py plan      --root $R
.venv/bin/python scripts/archive_sources.py fetch     --root $R --limit 200
.venv/bin/python scripts/archive_sources.py market    --root $R
.venv/bin/python scripts/archive_sources.py coverage  --root $R   # 写 $R/coverage.json
.venv/bin/python -m unittest tests.test_archive_sources
```

## 首批复核

首批主模型为 Claude Opus 5.5，WebFetch 使用 Haiku 5.5 辅助处理。复核后增加了发行人身份不符时停止采集、提交附件完整性检查，并保留非整数候选成交量。当前采集测试 16 项通过，仍未取得 SEC 原始提交文件，不能据此报告正式成绩。

精确行情窗口核验可独立复跑：

```sh
.venv/bin/python -m scripts.audit_market_archive --archive artifacts/benchmark-quality/claude/batch-01/archive --output artifacts/benchmark-quality/market-window-audit.json
```

该核验检查全部 20 题的 252 日预热、60 日区间、正价格、收盘时间和交易日覆盖；不替代公司行动、全部历史或财报档案检查。历史查询基础接口在 `quant_rsi/application/history.py`，模型工具在 `quant_rsi/tools/history-tools.v1.json`。当前尚未接入 Qwen 的正式运行器。
