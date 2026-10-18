# 发行人官方 SEC 镜像采集

本机访问 SEC 直连和 bulk 下载都返回 403，所以 `scripts/issuer_mirror.py` 改从发行人 IR 站点的 SEC 镜像采集申报原文。镜像只是 EDGAR 的二手副本，不能替代 `archive_sources.py` 的 SEC 路径，也不会让任何题目进入 ready 状态。

## 可用镜像

| 标的 | 镜像 | 状态 |
|---|---|---|
| INTC | `https://www.intc.com/filings-reports/all-sec-filings` | 200，已实现 |
| AAPL / NVDA / META / MU / AMZN / PYPL | 官方 filings 页 | 403，未实现 |
| MSFT | 外层页 200，iframe `microsoft.gcs-web.com` | 403，未实现 |

新增镜像的方法：在 `MIRRORS` 中登记 landing URL、允许的主机和题目窗口。如果页面不是 intc.com 的 `spr-*` 标记，还要另写解析器。所有新标记都必须先拿保存的真实页面做测试。

## 方法

- 存储复用 `archive_sources.Store`：原始字节按 sha256 内容寻址保存，`fetch-log.jsonl` 只追加，失败响应体也保存。`catalog.jsonl` 记录每个版本的来源 URL、`retrieved_at` 和哈希。
- 请求复用 `archive_sources.Fetcher` 的重试和缓存逻辑。`MirrorFetcher` 让所有主机共用一个请求槽，全局每秒最多 1 个请求。超时 20 s，5xx 最多重试 2 次；收到第一个 403 就熔断该主机，本次运行停止。
- `plan`：
  1. 抓取 landing 页，从页面自身的 `#sec-filing-filter` GET 表单和下拉框读出可选的 form 和年份。
  2. 对共享计划中的 6 种 form 逐一提交表单，再沿 pager 的 Next 链接翻页。Next 链接必须指向同一路径、带同一个 `form_type`，且 `page` 恰好加 1，否则停止并记录。翻页 URL 全部来自页面，不靠猜测。
  3. 每行取 `.spr-sec-form a`（主文档）、`.spr-sec-desc .spr-expandable a`（全部已列附件）和 `a[title='Download Raw XBRL Files']`（XBRL zip）。镜像 PDF、XBRL viewer 和 equisolve xlsx 都是派生物，只登记在 `inventory_only`，不下载。
  4. 日期 `MM/DD/YY` 的世纪只通过镜像年份列表（1994–2026）唯一确定。accession 取自内容路径，并与 `data-fancybox` 交叉核对。
- 检查项：页码 active 不符、非末页条数不足、同一 form 内 accession 重复（视为翻页漂移，判为不完整）、跨页或跨 form 的字段冲突、请求的 form 与返回的 form 不符、返回的 form 不在计划内、文件名冲突、链接不在镜像主机上。
- 范围：pager 遍历覆盖全部历史。filing_date 不晚于 `--horizon` 的文件进入队列；省略时按套件和 XNAS 日历计算最晚 terminal，当前为 2024-10-04。更晚的申报仍写入 index，但不下载。
- `fetch --limit N`：可以续跑，已缓存的文件跳过；以 403、404 或 410 失败的项默认不重试（加 `--retry-failed` 可重试）。图片默认跳过，用 `--include-images` 纳入。`--accessions` 和 `--roles` 用来指定下载顺序。
- `coverage`：报告每种 form 的 pager 完整性（含 truncated 和 pending_pages）、申报数、文件的已取/失败（含状态码）/pending、payload 检查结果，以及磁盘用量外推。外推值只来自已取样本，并标注了样本数。

## 口径与限制

- `accepted_at` 一律为 null，`accepted_at_source` 也为 null。镜像只给出申报日期，zip 内没有 SGML 头。以后可以按已定义的保守日期 fallback 编译，但本工具不做这一步，也不标记 ready。
- 镜像的 form 下拉框里没有 `10-Q/A`，因此无法判断该类申报是根本不存在还是镜像遗漏，作为缺口上报。`10-K405` 等历史变体同样不在下拉框中。
- pager 完整只说明镜像自身的列表已经遍历完，不证明镜像覆盖了 EDGAR 上的全部申报。镜像返回的字节也没有和 EDGAR 原件比对过。
- 镜像没有给出 CIK，CIK 50863 只是固定配置值。
- 本工具不做三表解析。

## 2026-10-17 本地采集记录

已遍历 61 页，截止套件最晚终点共有 538 份申报，排队 1,865 个文件。批量下载按 3 秒间隔完成 1,863 个文件，另外两个 URL 返回 403，失败记录和响应体保留。两个缺失文件都在同 accession 的已下载官方 ZIP 中找到同名成员，已另存成员字节与 ZIP/member 双哈希。这些副本没有冒充原 URL 的成功下载，也没有证明与该 URL 字节完全一致。

135 个 ZIP 中，103 个含 inline XBRL，32 个含传统 XBRL instance。下载完成不等于档案达到题目要求：公开时间、SEC 清单完整性、历史变体表单和其余发行人的档案仍需补齐。当前没有题目因此改为 ready。

进一步读取成员及检查 HTML 结束标签后，1 份 ZIP 因负成员偏移被拒绝，42 个 HTML 文件缺少结束标签。这是需要核实的内容缺口；缺标签本身不证明所有文件都被截断。损坏 ZIP 对应的 2020 年 10-K 主 HTML 也确实无法完整解析。`coverage` 会保留这些 payload 问题，不把 HTTP 200 视为内容完整性的证明。

可使用 `scripts/inventory_mirror_xbrl.py` 从队列和经哈希核对的本地字节生成解析清单。它实际读取 ZIP 成员，保留未下载、损坏和无法分类的条目；出现问题返回非零退出码并保存报告。该次清单的可解析分类为 102 份 inline、32 份 instance、1 份损坏，区别于仅从 ZIP 目录判断的 103/32。

## 命令

```
R=artifacts/benchmark-quality/claude/batch-03/archive
.venv/bin/python -I scripts/issuer_mirror.py plan --root $R --symbol INTC            # 全历史索引（加 --max-pages N 则只做采样，结果标为 truncated）
.venv/bin/python -I scripts/issuer_mirror.py fetch --root $R --symbol INTC --limit 200
.venv/bin/python -I scripts/issuer_mirror.py coverage --root $R --symbol INTC
.venv/bin/python -m unittest tests.test_issuer_mirror
```

## 2026-10-18：微软官方目录及 Word 副本

Claude 第 14、15 批从 [Microsoft 年报目录](https://www.microsoft.com/en-us/Investor/annual-reports)和各季度 earnings 页面取得 FY1996–FY2025 的 30 年目录，以及 FY2022–FY2024 的 3 份年报、9 份季报 Word 副本，共 11,883,580 字节。季度页地址由已观测模板生成，并明确标注为 locator；Word 地址则必须实际出现在已保存页面中。没有访问 Office viewer，也没有重试受阻的 SEC/gcs-web 主机。

固定的公开 URL、完整哈希、字节数、来源页 URL/哈希和未验证候选编号保存在 `configs/data-sources/msft-issuer-copies-20261018.toml`，可据此重取并核验同一版本；若供应商更换字节，不能覆盖旧哈希来凑出一致。原始文件、逐请求记录和父进程复核留在 `artifacts/benchmark-quality/claude/batch-14/`、`batch-15/`。

第 14 批触及轮数上限，按未完成记录；父进程恢复并复核其 15 份响应（含一次重定向，共 16 次请求）和 6 份 Word。第 15 批正常完成，12 次请求补齐其余 6 份季报。父进程复核全部文件的长度、SHA-256、ZIP CRC 和观测来源链接，并独立重算 12 个参考候选。

Word 正文尚未提取，身份仅按发行人 URL 的财年/季度标签和参考报告期匹配；季度标签用财年，不能当作同年的日历季度。它们是发行人 Word 副本，不等于 SEC as-filed 原件或全部附件。Last-Modified 普遍晚于申报日期，可能是后续上传，不能直接证明当时公开的字节版本。因此清单里的原文身份、历史字节和公开时间验证均为 false，仍不计入正式题目准入。更早的季报和实际下载的全部历史年报也没有因此补齐。
