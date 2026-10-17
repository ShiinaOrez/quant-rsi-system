# TeraflopAI/SEC-EDGAR 原文候选适配器

[TeraflopAI/SEC-EDGAR](https://huggingface.co/datasets/TeraflopAI/SEC-EDGAR) 是第三方整理的公开二手数据集（Apache-2.0）。适配器固定 revision `43de32c4c3cd4a56c26757345a4ec6db04fccda0`，代码在 `quant_rsi/adapters/hf_edgar.py`，命令行在 `scripts/hf_edgar_sources.py`。

## 字段角色

- `content`：数据集为每行保存的主文档字段，尚未核验为 SEC 原始字节，不能据此认定附件齐全。
- `text`：数据集派生的提取文本，不能替代 `content`，导出时不读取。
- `metadata_filer`：Parquet 内是 JSON 对象字符串，`json.loads` 一次得到对象（多发行人时可能为列表，按参考 CIK 唯一匹配）。
- `metadata_documents`：在 MSFT FY2022 10-K（`0001564590-22-026876`）这一行里，是只有一个条目的 JSON 列表，字段为 `type`、`sequence`、`filename`、`description`、`secsgml_size_bytes`，文件名 `msft-10k_20220630.htm` 与版本化索引的 `primaryDocument` 一致。这是首份核对样本；后续批量导出会逐行记录实际结构，不能从该样本推断其他申报的格式。

## 校验规则

- manifest 的 repo、revision 必须一致，且标为公开、非 gated；Parquet 路径必须是安全相对路径且不重复。URL 只能由固定 repo、revision 和 manifest 中的路径拼出。
- 元数据的 accession、filer CIK、form-type、filing date 必须与[版本化索引](submissions-reference.md)中唯一的 (accession, CIK) 参考行一致。空值、重复行、冲突行、错误身份一律拒绝，不会自动挑选其中一行。
- 带 `truncated_cells` 的 Dataset Viewer 行一律拒绝，即使该列表为空。
- 输出同时保留原字段字符串和解析结构，以及 dataset、revision、path、file_row_number（DuckDB 从 0 开始计数）和 manifest SHA-256。
- `accepted_at` 为空，`ready=false`，`agent_visible=false`。2026 年快照只作构建材料，不能直接交给代理。

## 读取边界

DuckDB 读取 Parquet 时以列块为单位：只取一行的 `content`，也要读完它所在行组的整个 content 列块。`extract` 先读 footer，把目标行组中所需列（content 与五个元数据列）的压缩大小加总，超过 `--max-row-group-bytes`（默认 100,000,000）即拒绝。这只是预检估计，不是网络流量硬上限；footer、HTTP 开销和 DuckDB 的预读不计入。`--timeout` 是 DuckDB 单次 HTTP 请求的超时，不是命令总时长，也不代表服务方的额度。

定位时先只投影 accession 列，命中后再按 `file_row_number` 读取元数据列，避免整列读取 `metadata_filer`。扫描是串行的：每个分片一份 receipt，仅复用 revision、路径、目标集、参考文件哈希和 manifest 哈希均一致的成功记录；每处理完一个联网分片至少等待 1 秒；单次最多 20 个分片；联网读取遇到第一个错误就写 `failure-*.json` 并以非零状态退出。HTTP 单次请求默认最多重试一次，可设 `--retries 0`；不自动重启失败批次。参数、来源或目录校验错误在 stderr 报告，可能尚未创建失败文件。输出目录中只要有本适配器之外的 JSON 文件（例如父进程的 `metadata-10k/`），命令就拒绝运行。所有输出都用独占方式创建，不覆盖已有文件。

### 同分片批量导出（extract-group）

同一发行人各年度在分片中通常相邻，常落在同一行组。逐份 `extract` 会重复下载同一个约 100MB 的 content 列块，因此 `extract-group` 一次处理同一 `--path` 下最多 20 个显式目标（`--targets` 为 JSON 列表，每项只含 `accession`、`cik`、`file_row_number`）：

1. 离线：每个目标须在参考 JSONL 中有唯一 (accession, CIK) 行；accession 或行号重复、键多余一律拒绝；输出文件已存在即拒绝。
2. 读正文前：一次查询读取目标行的五个元数据列，逐行核对 accession、CIK、form、filing date；缺行或身份不符即失败。
3. footer 预检：按目标实际所在的不同行组计 `CONTENT_COLUMNS` 压缩大小，同一行组只计一次，跨组求和；单组超过 `--max-row-group-bytes` 或总和超过必填的 `--max-total-bytes` 即拒绝。仍是预检估计，不是网络流量硬上限。
4. 一条参数化查询（`list_contains(?, file_row_number)`）读取 content 与元数据，不读 `text`，不读整个文件的其他行组。返回行须与目标一一对应。
5. 若 `metadata_documents` 中恰有一项文件名等于参考行 `primaryDocument`，其 `secsgml_size_bytes` 须等于导出字节数；不唯一或不一致即失败；字段缺失、非 JSON 或无整数大小时记为 `not_comparable`。这只是数据集内部一致性，不是独立 SEC 核验，也不说明附件齐全。
6. 全部行在内存中校验通过后才写文件（独占创建），最后写 `group-<分片>-<时间>.json` 回执。没有该回执的目录不能视为整批成功；失败写 `failure-group-*.json` 并以非零退出。`content_files_present` 记录实际存在的目标文件，不能把进入 export 校验阶段误记为已经写入正文。

适配器只加载已安装的 httpfs，关闭扩展自动安装与自动加载，不创建 secret，也不读取 HF token。

## 命令

```sh
.venv/bin/python -m scripts.hf_edgar_sources list-shards \
  --manifest artifacts/benchmark-quality/hf-edgar/dataset-manifest.json --form 10-K

.venv/bin/python -m scripts.hf_edgar_sources scan --manifest ... --form 10-K \
  --reference artifacts/benchmark-quality/public-index/import-2026-09-30/MSFT.jsonl \
  --reference-form 10-K --cutoff 2024-10-04 --receipts <新目录> --max-shards 2

.venv/bin/python -m scripts.hf_edgar_sources locate --manifest ... --path <分片> \
  --reference <JSONL> --accession <accession> --cik <CIK> --out <目录>

.venv/bin/python -m scripts.hf_edgar_sources extract ... --row-number <行号> \
  [--max-row-group-bytes N]

.venv/bin/python -m scripts.hf_edgar_sources extract-group --manifest ... --path <分片> \
  --reference <JSONL> --targets <targets.json> --out <新目录> \
  --max-row-group-bytes 134217728 --max-total-bytes 536870912 --timeout 45 --retries 0
```

## 当前状态

MSFT 该行已完成联网定位和身份校验。行组 2 的 content 压缩后为 106,112,978 字节，加上元数据列共 106,150,290 字节；默认 100MB 上限会拒绝。父进程随后显式使用 128MiB 上限，实际运行 `extract` 成功导出 6,215,854 字节，SHA-256 与此前独立查询路径逐字节一致。这是首份成功样本；后续批量成果见下文，主文档始终不代表完整提交。

2026-10-17 第 10 批实现 `extract-group` 并尝试 AMZN（分片 `1bb7360a-…-96`，行 126–130）。目标与参考行离线核对通过，但联网时 huggingface.co 的 TLS 握手持续失败（curl 同样失败，对照站点正常），停在元数据阶段，未下载正文，也未取得新的财报文件。当时 AAPL 组（分片 `5cf888c1-…-79`，行 361–364）也未取得正文；父进程随后通过完整本地分片继续，见下文。详见 `artifacts/benchmark-quality/claude/batch-10/handoff.md`。

## 完整本地 Parquet 的读取

主站 TLS 失败后，父进程从公开 HF-Mirror 取得 AMZN 固定 revision 的完整分片。文件 596,997,210 字节，SHA-256 `a3895d69c2c336f44f2f3f4131ba947f86be94658db700569d7f563ff84496ab`，与镜像返回的 LFS 指针一致。这是镜像传输完整性核验；未从上游独立重取指针，不据此声称 HF 或 SEC 已验证这些字节。

`LocalParquetReader` 只读取显式指定的本地文件。加载时检查 receipt 的 dataset、revision、path、镜像 URL、大小和完整 SHA-256；查询只能映射到这一份文件，不加载 HTTP 扩展。原始数据集 URL 与实际传输来源分开保留在每份导出记录中。此模式不发送用户凭据，也不会自动切换下载源。

```sh
.venv/bin/python -m scripts.hf_edgar_sources extract-group \
  --manifest <已保存的manifest.json> --path <固定revision内的Parquet路径> \
  --reference <索引JSONL> --targets <targets.json> --out <新目录> \
  --max-row-group-bytes 134217728 --max-total-bytes 536870912 \
  --local-parquet <完整Parquet> --transfer-receipt <传输校验记录.json>
```

真实 AMZN 2020–2024 年提交的五份年报已通过此命令一次导出，合计 11,992,340 字节；主文档文件名及长度与元数据一致。现有解析器得到 7,202 条事实，其中 19 条数词转换未支持，保留原文与错误；全部可见时间仍未绑定。正文与解析产物在 `artifacts/benchmark-quality/hf-edgar/parent-amzn-group/`。单组列块预算用于提取预检，不包含事先下载整个分片的传输量。

AAPL 分片也已取得：686,530,700 字节，SHA-256 `d7385e1240db300741261a942dd9da7abd1d20f360aa9160d0dede142dc2a5aa`，同样只与镜像 LFS 指针核对。首次 128MiB 预检拒绝该行组的 147,127,489 字节；改为显式 160MiB 后成功，总预算保持 512MiB。失败记录保留，没有提前写正文。

随后只读两份本地分片的 accession 列，得到参考索引内 AAPL 10 份（1994–1996、2017–2023）、AMZN 11 份（2014–2024）10-K；通过同一批量校验导出全部 21 份，合计 41,564,577 字节。现代 inline 文档 10 份共 13,431 条事实；其余 11 份旧格式保留原文，不假报解析成功。AAPL 数词转换错误 41 条及 nocontent 转换错误 2 条、AMZN 数词转换错误 19 条，均保留原文。产物在 `parent-aapl-all-local/` 和 `parent-amzn-all-local/`，每组都有成功回执；附件与公开时间仍缺。
