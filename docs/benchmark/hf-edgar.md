# TeraflopAI/SEC-EDGAR 原文候选适配器

[TeraflopAI/SEC-EDGAR](https://huggingface.co/datasets/TeraflopAI/SEC-EDGAR) 是第三方整理的公开二手数据集（Apache-2.0）。适配器固定 revision `43de32c4c3cd4a56c26757345a4ec6db04fccda0`，代码在 `quant_rsi/adapters/hf_edgar.py`，命令行在 `scripts/hf_edgar_sources.py`。

## 字段角色

- `content`：数据集为每行保存的主文档字段，尚未核验为 SEC 原始字节，不能据此认定附件齐全。
- `text`：数据集派生的提取文本，不能替代 `content`，导出时不读取。
- `metadata_filer`：Parquet 内是 JSON 对象字符串，`json.loads` 一次得到对象（多发行人时可能为列表，按参考 CIK 唯一匹配）。
- `metadata_documents`：在 MSFT FY2022 10-K（`0001564590-22-026876`）这一行里，是只有一个条目的 JSON 列表，字段为 `type`、`sequence`、`filename`、`description`、`secsgml_size_bytes`，文件名 `msft-10k_20220630.htm` 与版本化索引的 `primaryDocument` 一致。只看过这一行，不能推断其他行的格式；适配器只报告实际结构，不解释含义。

## 校验规则

- manifest 的 repo、revision 必须一致，且标为公开、非 gated；Parquet 路径必须是安全相对路径且不重复。URL 只能由固定 repo、revision 和 manifest 中的路径拼出。
- 元数据的 accession、filer CIK、form-type、filing date 必须与[版本化索引](submissions-reference.md)中唯一的 (accession, CIK) 参考行一致。空值、重复行、冲突行、错误身份一律拒绝，不会自动挑选其中一行。
- 带 `truncated_cells` 的 Dataset Viewer 行一律拒绝，即使该列表为空。
- 输出同时保留原字段字符串和解析结构，以及 dataset、revision、path、file_row_number（DuckDB 从 0 开始计数）和 manifest SHA-256。
- `accepted_at` 为空，`ready=false`，`agent_visible=false`。2026 年快照只作构建材料，不能直接交给代理。

## 读取边界

DuckDB 读取 Parquet 时以列块为单位：只取一行的 `content`，也要读完它所在行组的整个 content 列块。`extract` 先读 footer，把目标行组中所需列（content 与五个元数据列）的压缩大小加总，超过 `--max-row-group-bytes`（默认 100,000,000）即拒绝。这只是预检估计，不是网络流量硬上限；footer、HTTP 开销和 DuckDB 的预读不计入。`--timeout` 是 DuckDB 单次 HTTP 请求的超时，不是命令总时长，也不代表服务方的额度。

定位时先只投影 accession 列，命中后再按 `file_row_number` 读取元数据列，避免整列读取 `metadata_filer`。扫描是串行的：每个分片一份 receipt，仅复用 revision、路径、目标集、参考文件哈希和 manifest 哈希均一致的成功记录；每处理完一个联网分片至少等待 1 秒；单次最多 20 个分片；联网读取遇到第一个错误就写 `failure-*.json` 并以非零状态退出。HTTP 单次请求默认最多重试一次，可设 `--retries 0`；不自动重启失败批次。参数、来源或目录校验错误在 stderr 报告，可能尚未创建失败文件。输出目录中只要有本适配器之外的 JSON 文件（例如父进程的 `metadata-10k/`），命令就拒绝运行。所有输出都用独占方式创建，不覆盖已有文件。

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
```

## 当前状态

MSFT 该行已完成联网定位和身份校验。行组 2 的 content 压缩后为 106,112,978 字节，加上元数据列共 106,150,290 字节；默认 100MB 上限会拒绝。父进程随后显式使用 128MiB 上限，实际运行 `extract` 成功导出 6,215,854 字节，SHA-256 与此前独立查询路径逐字节一致。目前实测完整主文档一份，尚不代表完整提交或覆盖其他申报。
