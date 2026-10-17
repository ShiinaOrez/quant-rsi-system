# SEC 索引的公开网页存档采集

`scripts/wayback_sources.py` 从 Internet Archive 读取 SEC submissions JSON 的存档副本。它只访问 `web.archive.org`，不访问被阻断的 SEC 主机。

`verified` 表示下载字节与指定 URL、指定捕获时间的 CDX 摘要一致；它不证明与 SEC 直连原件一致，也不证明历史清单完整。输出保留来源说明，`benchmark_ready` 始终为 false。`index_complete_relative_to_selected_main` 仅说明所选主索引列出的分片是否齐备并通过合并检查。

## 采集与核对

1. 按 CIK 构造主索引 URL。历史分片只能来自该主索引的 `filings.files[]`，名称必须为同一 CIK 的 `CIK…-submissions-NNN.json`。股票代码、固定 CIK 和 JSON 身份要相符。
2. 查询 CDX，要求返回的原 URL 精确匹配。默认选择本次返回的最新 200 捕获；若指定 `--prefer-main`，必须找到该时间，不能悄悄换成其它版本。满页或 resume key 记为截断；目前不自动翻页，不能据此宣称枚举了所有捕获。
3. 通过 `id_` 下载，拒绝重定向。核对原始字节的 SHA-1/base32 与 CDX digest、Memento 时间、Link original，以及存在时的 Content-Length。
4. 原始传输字节和 gzip 解码字节分别保存 SHA-256。gzip 必须完整解压且无尾随数据。只匹配解码后摘要的其它存档约定暂不接受，报告 `digest_matches_decoded_not_raw`。
5. 校验主索引 CIK、ticker、必要列及分片文件名；下载每个分片，再核对条数、日期范围和重复 accession 的冲突。历史表单变体单独计数。缺片进入 `gaps.json`，不会只合并 recent 后宣称完整。
6. 按套件最晚 terminal 日期过滤输出。缓存复用时重新核对原始字节、保存的 CDX 对应行、捕获时间和解码后哈希。主索引与分片来自不同版本而出现数量或日期冲突时，应选取能相互核对的存档版本，并保留旧版及选择依据。

## 时间与可见性

`acceptanceDateTime` 保留原字符串；每行 `available_at=null`、`acceptance_time_status=raw_unverified`。末尾 Z 的解释须逐份与 SGML 受理头或 EDGAR index 的 Accepted 字段核对，不采用全局改时区规则。捕获时间、下载时间和报告期末均不作为公开时间。本脚本不生成交易代理可见的数据集。

## 请求与大小限制

- 串行请求，每次完成后至少间隔 2 秒。
- 首个 403/429 停止该主机；停止记录写入日志，五分钟内的新进程也会停止。剩余分片标记 not_attempted。
- 不自动重试，不更换身份、IP 或代理。
- CDX 响应上限 2 MB，回放原文 4 MB，解码后 40 MB；导入本地文件也检查对应大小，响应头上限 64 KB。
- 每请求按 90 秒预算逐块检查；单次阻塞读还受 socket 超时约束，这不是严格的墙钟上限。超时、部分读取和超大响应均不标成功，已收到的失败字节仍保存。

## 存储与命令

`--root` 使用 `archive_sources.Store`。`blobs/` 保存原文、解码数据和 CDX；`logs/fetch-log.jsonl` 追加请求记录；`captures.jsonl` 追加捕获来源与校验结果；`index/<SYMBOL>.json`、`gaps.json` 是派生输出。导入时也保存 CDX 原文，后续可独立复核来源关系。

```sh
.venv/bin/python -I scripts/wayback_sources.py import --root DIR --source-dir artifacts/benchmark-quality/claude/batch-06/sources
.venv/bin/python -I scripts/wayback_sources.py collect --root DIR --symbols AAPL NVDA MSFT
.venv/bin/python -I scripts/wayback_sources.py collect --root DIR --symbols AMZN --prefer-main 20260907131434
.venv/bin/python -I scripts/wayback_sources.py index --root DIR --symbols AAPL NVDA MSFT AMZN
```

`--symbols` 支持已固定身份的股票，或新标的 `SYMBOL=CIK`。`--max-shards N` 只限制本次尝试数，未尝试的分片仍是缺口。命令退出 0 表示操作完成，是否齐备须查看 gaps 和完整性字段。

尚未实现逐 accession 的主文档、附件、XBRL 与完整 SGML 采集，也未核对 Internet Archive 的使用条款。没有题目因为索引采集成功而进入 ready。
