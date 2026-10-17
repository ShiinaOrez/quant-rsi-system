# EDGAR filing index 页离线解析

`quant_rsi/adapters/edgar_index.py` 解析已保存的 `{accession}-index.htm[l]` 原始字节。它不访问网络、不执行脚本，只用标准库 `HTMLParser` 读取标记。调用方必须给出原 index URL、预期 CIK 与 accession，以及非空的来源说明（如存档捕获 URL、CDX 摘要、原文路径）。

## 输出

- `form`、`filing_date`、`accepted`、`period_of_report`、`declared_document_count`：每项保留解析值、原文 `raw`、页面字段名和源行号。
- `accepted` 只取页面 `Accepted` 字段，按 America/New_York 墙钟时间解释，输出带偏移的时间和 UTC。缺失、格式不符（如带 `Z`、只有日期）、重复且不一致、落在秋季回拨的重复小时或春季跳过的小时，均记为 blocking issue。不会用捕获时间、`Last-Modified`、submissions JSON 的 `Z` 或 filing date 推算。
- `documents`：Document Format Files 与 Data Files 两表逐行保留 sequence、description、name、type、size（空值为 `size_status=unknown`）、原 href、规范化原 URL、链接类型（`direct` 或 `ix_wrapper`）、角色与派生标记。
- `complete_submission_url`：完整 SGML 文本（`{accession}.txt`）链接。
- `source.raw_sha256` 与调用方来源说明；`issues`；`acceptance_metadata_ready` 表示受理元数据通过解析检查。`pit_ready` 与 `benchmark_ready` 恒为 false，`available_at` 恒为 null。

`declared_document_count` 是页面声明的申报文档数，可能多于 index 列出的行（AAPL 样本声明 69，列出 11 个带序号行）。`enumeration` 恒为 `index_listed_only`，不表示已枚举全部文档。

派生文件只按明确标记分类：描述含 `EXTRACTED`，或文件名为 EDGAR 渲染产物（`R<n>.htm`、`Financial_Report.xlsx`、`FilingSummary.xml`）。其它行为 `not_marked`，不声称是原件。所有行都保留，由上层选择。

## 身份与链接规则

- 原 index URL 必须是 `https://www.sec.gov/Archives/edgar/data/{cik}/{accession无横线}/{accession}-index.htm[l]`，CIK 与 accession 须和预期一致；页面 SEC Accession 与标题中的 accession 必须一致。这些不符会抛出 `EdgarIndexError`。accession 前缀可以是代理提交者，不要求等于 CIK，只记录 `accession_prefix_is_expected_cik`。
- 页面公司信息中必须出现预期 CIK；表单与 identInfo 的 `Type:` 冲突时阻断。
- 文档链接只接受同一 filing 目录下的 `/Archives/edgar/data/{cik}/{accession}/{文件名}`（相对根路径、`https://www.sec.gov` 或 `//www.sec.gov`）。`/ix?doc=` 只接受唯一的 `doc` 参数且其值为上述路径；不请求 viewer。拒绝外站、`http`、凭据或 `@`、任何 `%` 编码、反斜杠、`..` 或额外路径段、查询串、片段、相对路径和非 ASCII 字符。锚文本必须与链接文件名一致。
- 两张文档表以外的链接（导航、Interactive Data 按钮、公司检索）不算申报文件；未知标题的 `tableFile` 表记为 blocking 且不并入。

## 拒绝与问题

既无已知文档表、也无 `Accepted` 的页面（如限流页）直接抛出 `not_an_edgar_filing_index`。其余问题写入 `issues`，任一 blocking 都使 `acceptance_metadata_ready=false`：表未闭合、缺少 `END DOCUMENT DIV`/`END FILER DIV` 标记（截断）、缺表头或列数不对、重复表、同序号内容不同、同文件名不同序号、完整提交行缺失或冲突、序号/大小非数字、列出序号多于声明数、链接被拒等。完全相同的重复行和重复字段只记 warning，并保留全部行。页面没有 `</html>` 只记 warning：AAPL 样本本身以 `END FILER DIV` 结束，存档中记录的原始 Content-Length 与样本字节数一致。

Accepted 是受理时间，不能自动等同公开时间。[SEC 的说明](https://www.sec.gov/submit-filings/filer-support-resources/how-do-i-guides/determine-status-my-filing)指出，多数在 17:30 ET 之后传送的申报会取得下个工作日的 filing date，且次日才对外发布；不同表单存在例外，历史规则也会变化。解析器只保留受理元数据，不自行猜测准确发布时间。后续可见性策略须明确区分真实发布证据、受理代理和保守延迟，披露依据与不确定性，并按目标时期规则核验。

## 命令

```sh
.venv/bin/python -m unittest tests.test_edgar_index
```

存档样本存在时，测试用独立正则读取同一原文核对各行与 Accepted，并核对 SHA-256 与 batch-06 的 parent-verification。
