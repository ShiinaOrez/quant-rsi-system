# 财务事实抽取（inline XBRL 与传统 XBRL instance）

`quant_rsi/adapters/inline_xbrl.py` 从本地 SEC `*-xbrl.zip` 抽取 inline XBRL 1.1 事实，只用标准库。命令行入口是 `scripts/extract_financials.py`。抽取器只把原始披露转写成可审计的记录，不判断哪一个数字“正确”，也不判断哪一个是“最新”。

## 用法

```
.venv/bin/python -I scripts/extract_financials.py --receipts R.json --zip A.zip [--zip B.zip] --out-dir DIR
.venv/bin/python -I scripts/inventory_mirror_xbrl.py --root ARCHIVE_ROOT --symbol INTC --output INV.json
.venv/bin/python -I scripts/extract_financials.py --inventory INV.json --select instance --out-dir DIR
.venv/bin/python -I scripts/extract_financials.py --zip A.zip --accession 0000050863-24-000076 --source-url URL --out-dir DIR
  [--available-at 2024-04-26T16:05:00-04:00 --available-at-basis "SEC acceptance datetime"]
```

- accession 和 sourceURL 来自 receipts，按 ZIP 的 sha256 匹配；匹配不到或匹配多条时拒绝该文件。
- inventory 报告中的损坏和缺失项必须先核对。选择 `instance` 只运行已分类的传统档案，不代表整个档案集完整。
- 输出文件：
  - `facts-<acc>.json`：全部事实，以及 contexts、units、roles；
  - `issues-<acc>.json`：计数、问题、重复组、被拒成员；
  - `summary.json` / `summary.md`：Assets、NetIncomeLoss、NetCashProvidedByUsedInOperatingActivities 的全部期间和 context，权益相关行，以及跨申报比较。

## 每条事实保留的字段

- `concept`：QName、namespace、local name；
- 原始文本 `raw_text`，以及 `format`（QName 与 namespace）、`scale`、`sign`、`decimals`/`precision`、`nil`；
- `value`：Decimal 精确值的字符串；
- `context`：entity scheme 和 identifier、instant/duration/forever、segment 和 scenario 维度（explicit 或 typed）；
- `unit`：分子和分母 measure；
- 事实定位：`fact_id`、`hidden`、嵌套关系；
- `source`：accession、ZIP sha256、成员名、成员 sha256、sourceURL、行列号和元素路径；
- 时间与状态：`available_at`、`available_at_basis`、`pit_ready`、`status`/`errors`；
- `presentation_roles`：所在的展示角色。

## 数值规则（依据规范，不凭名称猜测）

- 计算方式：value = transform(text) × 10^scale；`sign="-"` 时取负（Inline XBRL 1.1 第 10.1 节、第 10.2.3 节）。
- 无 format 时，文本必须是非负的十进制数。
- nonFraction 的内容只能是一个文本节点，或一个嵌套的 nonFraction。嵌套时两者各算一条事实，并核对 format、scale、unitRef 是否一致。如果内部含有标签、脚注标记、`ix:exclude` 或注释，记为 `invalid_nonfraction_content` 等错误；值为 null，原文保留。
- 必须有 decimals 或 precision 中的恰好一个，nil 除外。nil 不能带内容，也不能嵌套。
- 支持的转换：
  - TR4（2020-02-12）的 `num-dot-decimal`：逗号、空格或不换行空格作千分位，点作小数点，不接受符号和指数；
  - TR4 的 `fixed-zero`：任意输入得 0；
  - TR3（2015-02-26）的 `numdotdecimal`、`zerodash`：分别按三位整数分组规则解析数值，或将规范列出的单个破折号转为零；不复用 TR4 较宽松的分组规则。规则来源：[XBRL Registry 3，含 2019-04-17 勘误](https://www.xbrl.org/Specification/inlineXBRL-transformationRegistry/REC-2015-02-26%2Berrata-2019-04-17/inlineXBRL-transformationRegistry-REC-2015-02-26%2Bcorrected-errata-2019-04-17.html)。
  - `ixt-sec:numwordsen`：支持明确列出的单词子集：`no`/`none`、`zero` 至 `nineteen`、整十词 `twenty` 至 `ninety`；忽略大小写并 collapse XML 空白，再按既有规则应用 scale/sign。复合数词、标点及错误拼写保留原文和错误，不猜测。依据 [SEC Filer Manual v45 §5.2.5.12](https://www.sec.gov/files/edgar/filermanual/archive/efmvol2-v45.pdf)；只实现该规则的子集。
- 其余 format 记为 `unsupported_format`，其余英文数词记为 `unsupported_transform_input`，都保留原文。同名转换必须匹配 namespace，未验证的 TR1/TR2 不自动作为别名接受。
- concept、format 和维度的 QName 目前要求带前缀；前缀未绑定或在局部重新绑定都记为错误。unit measure 另按下述规则支持默认命名空间。
- 缺失或无效的 context/unit、重复的资源 ID 都是错误。ix:fraction 和 ix:tuple 记为不支持。
- nonNumeric 按 continuation 链拼接文本，并剔除 `ix:exclude`。带 format 的 nonNumeric（日期、布尔值等）不做转换：状态为 `raw_only`，值为 null。

## 重复与重述

- 同一文档内，若 concept、context 内容和 unit 都相同，就分为一组：值相同的组标为 consistent，值不同的组标为 inconsistent，组内有错误事实的标为 unresolved。所有事实都保留，不做取舍。
- 跨申报比较有两种口径：
  - 精确 QName 口径；
  - `us_gaap_local_name` 口径：把不同年份 us-gaap 中同名的 concept 视为同一个。这只是命名惯例，没有经过验证。
- 跨申报的差异只报告，不认定为重述，也不覆盖任何一方。

## 时间

- `available_at` 只能由调用方显式传入，而且必须带时区和书面依据（basis）。
- 不从报告期末推断时间，也不使用 retrieved_at。
- 未传入时，所有事实 `available_at=null`、`pit_ready=false`。
- `task_ready` 恒为 false；`accepted_at` 目前为 null。

## 安全限制

- 不调用 extractall。ZIP 总大小、成员数、单个成员大小、总解压量、压缩比都有上限，并且实际读取的长度必须与 header 中记录的一致。
- 不安全的成员名、加密成员、重名成员一律拒绝。
- 含 DOCTYPE、实体声明或外部实体的 XML 一律拒绝。
- 只接受本地路径，带 URL 的路径会被拒绝。外部 taxonomy 不抓取。
- 遇到多文档 inline 集合，或旧的 iXBRL 1.0 namespace，整份拒绝。

## 展示角色（三表结构证据）

- 从本地 xsd 的 `presentationLinkbaseRef` 找到 `_pre.xml`，读取每个 presentationLink 的原始 roleURI，以及 xsd 中 roleType 的 definition 原文。
- 角色与事实按 concept 绑定，不按 context 绑定。
  - 本地 xsd 中的元素按 id 精确对应，basis 为 `local_schema_element_id`；
  - us-gaap 等外部 taxonomy 中的元素，按 href 片段 `prefix_LocalName` 的惯例对应，basis 为 `external_fragment_convention_unverified`。
- 同一个 concept 可能出现在多个角色里。例如 Assets 同时出现在 “Statement - Consolidated Condensed Balance Sheets” 和 “Non-Controlling Interests - Narrative (Details)” 中，所以不能据此把某个事实硬归到某张主表。

## 传统 XBRL 2.1 instance（`quant_rsi/adapters/xbrl_instance.py`）

2011–2019 年的 Intel ZIP 里不是 inline 文档，而是根元素为 `xbrli:xbrl` 的独立 `.xml` instance。新适配器复用 `inline_xbrl.py` 的 ZIP/XML 安全读取、context/unit 解析、重复组、展示角色绑定和 provenance。

入口：`xbrl_instance.extract_any_zip` 是显式 dispatcher。只有 inline 文档时走 `inline_xbrl.extract_zip`，只有 instance 时走 `xbrl_instance.extract_zip`，两者同时出现则整份拒绝（`mixed_inline_and_instance_refused`），不替调用方选。`.xml` 成员只在根元素确为 `xbrli:xbrl` 时才算 instance，linkbase 和其它 XML 一律忽略。ZIP 内有多个 instance 时全部抽取，并记 `multiple_instance_documents` 警告。`extract_financials.py` 一律走 dispatcher；`--inventory INV --select instance|inline|all` 可直接从 ZIP 清单取文件（清单 filings 充当 receipts，仍按 sha256 匹配并核对 accession），`--summary-only` 不写逐份 facts 文件。

规则（XBRL 2.1 REC 2003-12-31 含 2013-02-20 勘误：<https://www.xbrl.org/Specification/XBRL-2.1/REC-2003-12-31/XBRL-2.1-REC-2003-12-31+corrected-errata-2013-02-20.html>；XML Schema 1.0 Part 2 第 3.2.3 节 decimal：<https://www.w3.org/TR/xmlschema-2/#decimal>）：

- 带 `@contextRef` 的根级子元素是 item。§4.6.2 规定数值 item 必须有 unitRef、非数值 item 不得有，所以 kind（`numericItem`/`nonNumericItem`）只按这条结构规则区分，`kind_basis` 标明出处。taxonomy 类型（monetary、shares、日期等）不由 unit 猜测；外部 taxonomy 不抓取，`item_type` 恒为 null。
- **不缩放**：元素文本就是完整数值。decimals/precision 只描述精度（§4.6.4/§4.6.5：`INF` 表示文本即精确值），`scale`/`sign`/`format` 恒为 null。`66089000000` 配 `decimals="-6"`，值仍是 66089000000。
- 数值文本必须符合 xs:decimal 词法（先做 collapse 空白）：可带一个前导 `+`/`-`，有无小数点均可。`-1234` 就是负数。`value` 是规范化后的精确 Decimal（`0.0490` 和 `0.049` 是同一个值，写成 `0.049`），`raw_text` 保留原文。
- 指数写法、`INF`、`NaN` 只在 concept 类型派生自 float/double 时才合法，离线无法确认，所以这类事实为 `raw_only`，带 note `float_lexical_requires_float_or_double_type_unverified`。千分位逗号、括号负数、货币符号、空值等记为错误，原文保留。
- §4.6.3：非 nil 的数值 item 必须恰好带 decimals 或 precision 之一；两者都有记 `both_decimals_and_precision`，都没有记 `decimals_or_precision_required`。nil item 不得带这两个属性，也不得有内容。非数值 item 带 decimals/precision 也是错误。
- 非数值 item 的文本原样保留，不做规范化或转换（类型未知），另记 `text_sha256`。纯数字文本若没有 unitRef，不当作数字处理。
- tuple（无 contextRef 的根级子元素）记 `unsupported_tuple`，内部 item 不抽取。fraction 和带子元素的 item 记为错误。值内含注释也是错误。
- 文档内所有 `@id` 必须唯一（xs:ID）。重复的 id 记 `duplicate_id`，被引用到的 context/unit 标为有歧义。
- `link:footnoteLink` 按 loc→arc→footnote 挂到事实的 `footnotes` 上，保留原文、xml:lang、role 和 arcrole。指向不存在事实的 loc 记警告。
- xml:lang 从祖先元素继承。
- 计数字段是 `instance_documents`、`numericItem`、`nonNumericItem`；`inline_documents` 恒为 0，不会把 instance 计成 inline。
- `inline_xbrl.py` 只做了几处小改：增加 `NUMERIC_KINDS`，让重复组和跨申报比较也认 `numericItem`；跨申报 `us_gaap_local_name` 口径和 `extract_financials.py` 增加识别 `http://fasb.org/us-gaap/YYYY-01-31`（2011–2018）和 `http://xbrl.us/us-gaap/2009-01-31`。
- 重复组里的 inconsistent 也包括只差舍入的情况，例如同一事实的 4482000000（decimals=-6）和 4500000000（decimals=-8）。按 §4.6.6 这类值在精度范围内可能一致，但这里不判断，只报告。
- Intel 2013-07 到 2019-02 的 instance 用 `NetCashProvidedByUsedInOperatingActivitiesContinuingOperations` 报经营现金流。summary 把它作为独立 concept 列出，不与 `NetCashProvidedByUsedInOperatingActivities` 合并。

## unit measure 的默认命名空间

XBRL 2.1 §4.8.2 把 measure 定义为 `xsd:QName`。[XML Schema §3.15.3](https://www.w3.org/TR/xmlschema-1/#src-qname) 规定无前缀的 QName 使用作用域内的默认命名空间。因此根元素声明 `xmlns="http://www.xbrl.org/2003/instance"` 时，`<measure>shares</measure>` 与带 `xbrli:` 前缀的写法可指向同一单位。

解析器只使用实际声明，不从 `shares` 或 `pure` 猜命名空间；缺少默认绑定仍记错误，局部重绑定仍按既有保守策略拒绝。默认绑定为其他 URI 时保留该 URI，不冒充 XBRL 单位；taxonomy 合法性尚未校验。此修复用于共享 unit 解析，不放宽其他 QName 调用点。

2026-10-17 重跑 Meta、Amazon 的 6 份官方 IR ZIP 样本，数值错误从 700 条降为 0。另用 ElementTree 和 Decimal 独立核算 5,729 条数值事实及 5,873 个单位组成，无差异；6 份的 CIK、表单、报告期和主文档名均与候选索引一致。修复前后的产物分别保留在 `artifacts/benchmark-quality/claude/batch-11/q4/` 和 `q4-parent-unitfix-v1/`；accession 仍为候选，公开时间仍为空。

## 已知缺口

- 没有处理 1994–2010 年的纯文本和 HTML 财报，也没有声称它们已经被抽取。XBRL zip 最早一份是 2011 年。
- 传统 instance：tuple、fraction、float/double 词法都没有支持；不加载 taxonomy schema，所以无法校验 item 类型、period type 或 balance；计算/维度校验也没有做。
- TR3 仅支持上述两项数值转换，TR1/TR2 未实现。2026-10-17 对 103 份 inline ZIP 的检查中，102 份可解析，合计 25,950 条事实；新增转换将 `unsupported_format` 从 6,889 条降为 8 条。2026-10-19 在经过校验的 102 份清单上重跑，新增单词子集解决另外 15 条数词错误；现余 8 条 `unsupported_format`，数值状态为 22,466 条 ok、37 条 nil、8 条 error。一份 2020 年 10-K 的 ZIP 成员偏移损坏，镜像主 HTML 也无法完整解析，均保留为缺口；不修改原件来凑出成功结果。所有事实仍未绑定公开时间。
- 复合英文数词、nonNumeric 的转换、calculation/definition linkbase、三表勾稽都没有做。只做了一项检查：每份申报中，无维度的 Assets 与 LiabilitiesAndStockholdersEquity 是否相等。
- context 的比较是字符串级别的，日期格式不做归一化。
- 镜像字节没有与 EDGAR 原件比对。accepted_at 未知。

## 单个英文数词的真实数据核对（2026-10-19）

对 AAPL、AMZN、PYPL 的 30 份本地主文档重新检查，其中 15 份带 inline namespace，共 24,433 条事实。新增单词子集分别恢复 41、17、47 条数值，共 105 条；仍有 6 条 `ixt:nocontent` 错误，未给它们补数值。其余 15 份旧格式正文不计入解析成功。此前把 AAPL/AMZN 的 62 条错误写成 60 条数词加 2 条 nocontent 是分类错误，实际为 58 加 4，现已更正。

其中 45 条数词事实可与另一来源的传统 XBRL instance 按候选 accession、fact ID、concept QName、contextRef 及 Decimal 值比较，全部一致；包括 26 条新增非零转换和 19 条原有零值对照。该核对不等于完整 context/unit/维度、SEC 原件或公开时间核验。产物在 `artifacts/benchmark-quality/numeric-words-v1/`，所有新增事实仍不可向正式 benchmark 开放。

## Micron 存档实例（2026-10-19）

从已通过 CDX digest 核对的官方历史详情页，沿原页面直接列出的静态文件链接，取得 accession 候选 `0000723125-24-000027` 的 XBRL instance。Claude 的首取因 25 秒超时截断，不计成功；父进程重新下载在 90 秒时仍差 38,042 字节，随后用标准 HTTP Range 补取，核对 206 Content-Range 后拼接。完整的 2,097,820 字节与先前取得的 CDX SHA1 digest 一致，原始分段、响应头和错误日志全部保留。

使用现有安全 XML 解析器得到 1,646 条事实，其中数值 1,447 条 ok、2 条 nil、0 条 error；另用 ElementTree 与 Decimal 核算全部 1,447 条非空数值，无差异。DEI CIK、表单和报告期与参考记录一致，全部 context 的发行人标识也均为 723125。未加载外部 taxonomy 或创建声称来自发行人的 ZIP；独立 instance 缺少展示角色绑定，保留 `schema_ref_not_followed` 提示。

源版本与哈希见 [Micron 实例清单](../../configs/data-sources/mu-archived-instance-20261019.toml)。下载、分段校验与抽取产物在 `artifacts/benchmark-quality/mu-archive-instance-retry-v1/`。存档捕获时间晚于财报申报时间，不用它补写历史 `available_at`；正式题目仍不开放这些事实。

Claude 第 17 批还取得该申报的 schema 和 definition linkbase，父进程复核摘要与安全 XML 根元素通过。标为 PRE 的链接却返回与 schema 完全相同的字节；CAL 链接的截断内容为 label linkbase，不能按链接文字当作对应文件。错误映射与超时均保留，未据此绑定展示角色或声称三表结构齐全。

该份年报申报于 2024-10-04，晚于现有三道 MU 题目的交易窗口，因此仅作归档与解析验证，不能用于填充那些窗口的可见信息。后续优先补 FY2022/FY2023 历史申报；本条来源覆盖不增加题目 ready 数。

## Micron FY2022 与 FY2023（2026-10-19）

第 18 批优先补历史窗口相关的两年年报。FY2022 实例通过一段 200 GET 与一段严格核对 Content-Range 的 206 响应取得；完整 2,791,127 字节与 CDX digest 一致。父进程重新校验每段、拼接结果和整体摘要后，用安全 XML 解析器得到 1,628 条事实；数值为 1,449 条 ok、2 条 nil、0 条 error。另用 ElementTree 与 Decimal 独立核算全部 1,449 条非空数值，无差异。全部 405 个 context 的 CIK 一致，DEI 表单与报告期也与参考记录相符。保存一条未加载 schema 的提示，不声称 taxonomy 或三表角色校验已完成。

[固定来源清单](../../configs/data-sources/mu-fy2022-archived-instance-20261019.toml)保留实际捕获地址和字节摘要；原始分段、组装回执及父进程抽取在 `artifacts/benchmark-quality/claude/batch-18/`。捕获时间晚于原申报，`available_at` 仍为空。

FY2023 的较早捕获完整下载后只有 1,048,576 字节，虽与其 CDX digest 相符，但 XML 在正文中间结束，不能当完整财报。较晚捕获最初取得 1,671,168 字节后超时；随后原捕获与 revisit 捕获之间出现 302 循环，父进程复查也未恢复。两份前缀的前 1 MiB 一致，只作为错误证据，不输出正式事实。来源截断与传输截断分开记录，存档哈希一致不替代文档完整性校验。

另对已取得的 FY2022、FY2024 实例各核对两个无维度时点的 Assets 与 LiabilitiesAndStockholdersEquity，四项均相等；key facts 与上下文保留在各自父进程目录的 `key-facts-review.json`。这只是单项恒等式检查，不代替三表、维度或 taxonomy 校验。

## Micron 主文档与实例交叉核对（2026-10-19）

第 19 批取得 20 份语料主文档，包括完整 FY2023 正文。五份 inline 财报抽取 8,386 条事实、7,466 条数值事实，数值错误为 0；十五份旧格式只保留正文。父进程另外核算实际源文件大小、摘要、索引身份和整组导出回执；Claude 派生汇总中的 `filing_date` 因字段名误用而为空，父进程报告与固定清单使用已核对的 `reference.filing_date`，保留原汇总供审计，不把申报日期当成公开时间。

FY2022、FY2024 与此前公开存档的完整实例逐条核对 fact ID、concept QName、contextRef、期间、发行人、显式维度、单位和 Decimal，合计 2,896 条非空数值及四条 nil 相符。跨来源有 321 条显式维度排列差异、400 条单位 QName 显示前缀差异，二者有重叠；原始结果保留。比较时只对同一 segment/scenario 内无重复轴的显式成员集合忽略排列顺序，单位按已解析的命名空间和本地名比较；不改写事实，也不推断默认成员或 typed dimension 等价性。该范围依据 [XBRL Dimensions REC §3.2](https://xbrl.org/specification/dimensions/rec-2012-01-25/dimensions-rec-2006-09-18%2Bcorrected-errata-2012-01-25-clean.html) 和 [XBRL 2.1 §4.8.2](https://www.xbrl.org/Specification/XBRL-2.1/REC-2003-12-31/XBRL-2.1-REC-2003-12-31+corrected-errata-2013-02-20.html)。这只是声明范围内的跨实例对照，不是完整 c-equal 校验。

父进程的源与事实报告在 `claude/batch-19/parent-review/`；FY2020、FY2021、FY2023 尚无独立实例对照，所有公开时间仍为空。FY2024 继续按先前限制排除在当前 MU 题目窗口之外。

## NVIDIA 数值子集复核（2026-10-19）

第 20 批的 21 份正文中，五份 inline 财报共抽取 6,671 条事实；5,891 条数值事实中 5,880 条 ok、9 条 nil、2 条 error。两条错误均在 FY2020，格式为 TR3 `nocontent`，保留空原文与错误，不替换为零。16 份旧格式未进行结构化抽取。申报日期已正确读取 `reference.filing_date`，所有可见时间仍为空。

Claude 用独立的 ElementTree 命名空间作用域跟踪和 Decimal 计算核对明确子集：唯一 fact ID、无子元素、非嵌套、非 exclude、非 nil 的 nonFraction，格式限于未指定、TR4 num-dot-decimal/fixed-zero 和 TR3 numdotdecimal/zerodash，再按 scale/sign 计算。父进程审阅该函数，并隔离其输出写入主程序后对实际原文和事实文件复跑，5,756 条数值无差异。其余按原始 nil 属性跳过 11 条（其中两条因不支持格式在生产解析中为 error）、数词转换 18 条、带子元素 55 条、嵌套子事实 51 条，未声称这些均通过独立数值校验。

同一独立核对还在相同发行人、同一无维度时点、相同 us-gaap namespace、单一 USD 单位下比较 Assets 与 LiabilitiesAndStockholdersEquity，十项均相等；不等同于三表或 taxonomy 校验。原始与复跑结果分别位于 `claude/batch-20/parsed/`、`parent-review/`，来源清单见 [NVIDIA 主文档](../../configs/data-sources/nvda-hf-annual-20261019.toml)。
