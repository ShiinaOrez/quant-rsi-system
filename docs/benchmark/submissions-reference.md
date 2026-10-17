# 版本化 SEC 申报索引

使用 Ian D. Gow 发布的 [SEC submissions reference data，2026-09-30](https://github.com/iangow/sec_submissions_data/releases/tag/2026-09-30)，补充网页存档未收录的历史索引。数据整理结果采用 CC BY 4.0；原始 SEC 申报文件保留其原有权利。配置在 `configs/data-sources/sec-submissions-2026-09-30.toml`，固定版本、manifest 和文件 SHA-256，避免 `latest` 随时间变化。

原始来源和方法见作者的[数据说明](https://iangow.github.io/sec_submissions/data/)与[审计说明](https://iangow.github.io/sec_submissions/audit/)。发布者的抽样审计不等于逐条核实我们所需的申报。`unresolved_raw_as_eastern` 明确表示未解决的兼容性推定，不能直接作为可信发布时间。其他 provenance 也须保留具体依据，不能统一标成独立验证成功。

## 本地导入

先下载配置所列的 `manifest.json` 与 `filings.parquet` 到被忽略的 artifacts 目录。仅在下载完成后重命名 `.part` 文件；不要把部分下载交给其他流程。索引约 1.24 GB，不需要下载原始 SEC ZIP 或全部公司财报。

```sh
.venv/bin/python -m pip install 'duckdb==1.4.1'
.venv/bin/python -m scripts.import_submissions_reference \
  --manifest artifacts/benchmark-quality/public-index/2026-09-30/manifest.json \
  --filings artifacts/benchmark-quality/public-index/2026-09-30/filings.parquet \
  --output artifacts/benchmark-quality/public-index/import-2026-09-30
```

导入器离线检查 manifest 哈希、版本、授权标记、Parquet 大小、完整哈希、总行数和字段类型，再按题目固定 CIK 筛选八家公司。完整索引行保留原字段和来源；同一发行人出现重复 accession 时拒绝导入。一个 accession 可能涉及多个 CIK，不能按 accession 前缀推断发行人。

每家公司输出全快照 JSONL，另在报告中按套件终点统计申报和历史表单变体。输出仅供构建使用，包括终点之后的记录；绝不能直接挂到模型历史查询端。环境编译仍须按决策时钟和经审核的公开时间筛选。

时间戳证据分为：缺失、未解决、已识别的上游支持规则、未知规则。未知值不会自动归为可信；所有行的 `independently_verified_publication_time` 初始均为 false。导入也不会将任何题目标成 ready。

完整索引仍需与已有网页存档、发行人镜像和原文交叉核对。财报正文、附件、三表勾稽、时点查询及行情核对分别通过后，才可能满足题目准入。

## 原文候选来源

另行调查 [TeraflopAI/SEC-EDGAR](https://huggingface.co/datasets/TeraflopAI/SEC-EDGAR)，本次查看的仓库 revision 为 `43de32c4c3cd4a56c26757345a4ec6db04fccda0`。它包含主文档 content 和提取文本，但不能凭数据卡认定所有附件齐全。Dataset Viewer 预览会截断长字段，必须检查 `truncated_cells`；截断内容不能冒充完整原文。服务端精确查询和远程 Parquet 列筛选仍在验证，不作为已完成的覆盖。

已用远程 Parquet 行筛选取得 MSFT accession `0001564590-22-026876` 的 content 字段，UTF-8 为 6,215,854 字节，SHA-256 `cc48e53a4052180eb7519599fc43f83f393baf5c0111d0511a514a6b751c73c5`。其 `metadata_documents` 仅列主文档，声明的长度与取得字节一致；这仍不证明完整提交或与 SEC 原字节相同。现有解析器抽取 1,819 条事实，独立 XML/Decimal 计算核对 1,549 条数值及空值无差异；未绑定可见时间。记录位于本地 `artifacts/benchmark-quality/hf-edgar/parent-msft-2022/`。

补充来源筛选：`ZipLime/sec-8k-events` 的[公开说明](https://huggingface.co/datasets/ZipLime/sec-8k-events)表明正文窗口从 2026 年起，不能拿其历史元数据补作本套件的 2022–2024 正文；`achaljhawar/secsearch` 的[说明](https://huggingface.co/datasets/achaljhawar/secsearch)明确不含 XBRL、附件受限，且检索会排除被修订的旧版，不能直接作为历史财务档案。
