# 存档 feed 的显式传输策略

2026-10-18。实现为 `quant_rsi/adapters/archived_q4.py`、`scripts/archived_q4_sources.py`；Apple 的固定输入策略在 `configs/data-sources/aapl-q4-20200730.toml`。

普通 Q4 计划仍拒绝 HTTP URL。新入口先校验整份捕获的长度、SHA-256 和 CDX SHA-1，再生成独立的 HTTPS 候选。它只允许同主机、同路径的协议改写，查询必须为空；主机须在该发行人的名单里，CIK 路径、UUID 和文档扩展名都要通过原有检查。未知主机、错误 CIK、用户凭据、查询参数及重定向均不能借此获得许可。

原 `url`、原始 feed 行和哈希保留；`fetch_url`、`transport_operation` 是单独字段。下载回执还记录 feed 哈希、策略文件哈希和原 URL。缓存只有身份、实际 URL、完整元数据和内容哈希都匹配才复用。成功请求只证明取得了 HTTPS 候选的字节，不证明它等于 HTTP 历史版本或 SEC 原件。

```sh
.venv/bin/python scripts/archived_q4_sources.py plan --capture <保存的原始feed载荷>
.venv/bin/python scripts/archived_q4_sources.py fetch --max-requests 60
.venv/bin/python scripts/archived_q4_sources.py parse
```

默认产物为 `artifacts/benchmark-quality/aapl-archive-https-v1/`，不提交原始数据。计划和抓取均可复核，解析复用现有 ZIP 安全检查和 XBRL 适配器；常规解析器按 catalog 中的发行人读取参考清单，不再把 Meta、Amazon 写死在解析循环里。三家的原始获取入口仍各自独立。

## Apple 实测

56 次请求均成功，共 9,960,409 字节；56 份 ZIP 均可解析，共 44,319 条事实。数值正常 39,635 条、nil 110 条、数值解析错误 0。DEI CIK 全部一致；另复核 10,483 个 context 的实体编号，也均指向 AAPL。55 份表单、54 份报告期与候选索引逐字段一致；14 份可用 `_htm.xml` 名称核对主 HTML，42 份传统 instance 不适用该推导。

三份需要上下文复核：

| FilingId | 候选 accession | 观测 |
|---|---|---|
| 7001526 | 0001193125-10-012091 | 索引为 10-K/A；DEI 为基础表单 10-K，同时 AmendmentFlag=true。保留精确表单不一致，不静默去掉 `/A`。 |
| 9238646 | 0001193125-13-170623 | 8-K 索引 reportDate 为 2013-04-24，DEI 财务期间为 2012-09-29。 |
| 10432804 | 0001193125-15-023732 | 8-K 索引 reportDate 为 2015-01-28，DEI 财务期间为 2014-09-27。 |

8-K 事件日期与所附财务报表期间不是必然同义字段；这些差异需要结合申报正文解释，不能仅凭日期断定拿错文件，也不能报告为完全一致。核对器因此保留精确字段结果、修订标记的补充证据及 `requires_context_review`，说明文字不再无条件声称“内容一致”。初始结果与复核后的 `parse-summary-v2.json`、`context-reviews.json` 分别保存。

这些文件仍只有 accession 候选；`available_at` 为空、`ready=false`。捕获中的最新申报仍为 2020-05-12，相对套件终点参考清单的 52 行缺口没有因此消失。
