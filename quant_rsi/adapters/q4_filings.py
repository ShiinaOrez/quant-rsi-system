"""Offline planner for issuer IR (Q4 platform) SEC filing feeds.

Input is a saved ``GetEdgarFilingList`` JSON response. Every planned row keeps the
raw feed fields, the source blob sha256 and the row position. Q4 ``FilingId`` and
``EdgarFilingId`` are not SEC accession numbers and are never turned into one; an
accession is only a *candidate* from a unique (issuer, form, filingDate) match
against a fixed reference index, unless a saved official detail page links to an
SEC archive path that passes strict CIK/form/date checks. Nothing here is ready.
"""
import re
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import parse_qsl, urlsplit

PLAN_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A")
CDN_HOST = "d18rn0p25nwr6d.cloudfront.net"
# Observed independently in the official META feed and the pinned AAPL capture.
# Each issuer is admitted separately; this does not admit AMZN or unknown CIKs.
LEGACY_META_CDN_HOST = "d1lge852tjjqow.cloudfront.net"
ISSUERS = {
    "AAPL": {"cik": 320193, "origin": "https://investor.apple.com",
             "cdn_hosts": (CDN_HOST, LEGACY_META_CDN_HOST),
             "detail_path": "/sec-filings/sec-filings-details/default.aspx"},
    "META": {"cik": 1326801, "origin": "https://investor.atmeta.com",
             "cdn_hosts": (CDN_HOST, LEGACY_META_CDN_HOST),
             "detail_path": "/financials/sec-filings-details/default.aspx"},
    "AMZN": {"cik": 1018724, "origin": "https://ir.aboutamazon.com",
             "cdn_hosts": (CDN_HOST,),
             "detail_path": "/sec-filings/sec-filings-details/default.aspx"},
}
# Q4 DocumentType -> (role, is SEC-original bytes, allowed extensions)
DOCUMENT_ROLES = {
    "ORIG": ("original_filing", True, ()),
    "CONVPDF": ("derived_pdf_q4_conversion", False, ("pdf",)),
    "CONVTEXT": ("derived_text_q4_conversion", False, ()),
    "XBRL": ("xbrl_zip_not_full_attachment_set", False, ("zip",)),
    "XBRL_HTML": ("xbrl_html_unverified_role", False, ("html",)),
    "XLS": ("derived_financial_report_xls", False, ("xls",)),
    "REDLINE": ("redline", False, ("pdf",)),
    "PDF": ("pdf_unverified_origin", False, ("pdf",)),
}
FEED_FIELDS = ("DocumentList", "EdgarFilingId", "FilingAgentId", "FilingAgentName", "FilingDate",
               "FilingDescription", "FilingId", "FilingTypeId", "FilingTypeMnemonic", "LinkToDetailPage",
               "ReceivedDate", "ReportPersonId", "ReportPersonName", "StockExchange", "StockSymbol")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SEC_ARCHIVE = re.compile(r"https?://www\.sec\.gov/Archives/edgar/data/([0-9]{1,10})/([0-9]{18})(?=/|[\s\"'<>]|$)(?:/[^\s\"'<>]*)?")


class FeedError(ValueError):
    pass


def cik10(cik):
    return f"{int(cik):010d}"


def issuer(symbol):
    if symbol not in ISSUERS:
        raise FeedError(f"unknown issuer {symbol!r}")
    return ISSUERS[symbol]


def parse_filing_date(text):
    """'MM/DD/YYYY 00:00:00' -> 'YYYY-MM-DD'. A non-midnight time is refused (date-only field)."""
    m = re.fullmatch(r"([0-9]{2})/([0-9]{2})/([0-9]{4}) 00:00:00", text or "")
    if not m:
        raise FeedError(f"unexpected FilingDate {text!r}")
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError as err:
        raise FeedError(f"unexpected FilingDate {text!r}") from err


def check_cdn_url(url, symbol, extensions):
    """Return (ok, reason). Only https CDN paths under this issuer's 10-digit CIK."""
    if not url:
        return False, "empty_url"
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "unparseable"
    if parts.scheme == "http":
        return False, "http_not_upgraded"
    if parts.scheme != "https":
        return False, "scheme"
    if parts.username or parts.password or "@" in parts.netloc:
        return False, "credentials_in_url"
    if parts.netloc not in issuer(symbol)["cdn_hosts"]:
        return False, "foreign_host"
    if parts.query or parts.fragment:
        return False, "query_or_fragment"
    m = re.fullmatch(rf"/CIK-([0-9]{{10}})/({_UUID})\.([a-z]+)", parts.path)
    if not m:
        return False, "path_shape"
    if m.group(1) != cik10(issuer(symbol)["cik"]):
        return False, "cik_mismatch"
    if m.group(3) not in extensions:
        return False, "extension_not_allowed_for_type"
    return True, None


def detail_url(link, symbol, filing_id):
    """Absolute detail URL on the issuer's own origin, or (None, reason)."""
    info = issuer(symbol)
    if not link:
        return None, "empty_url"
    parts = urlsplit(link)
    if parts.scheme or parts.netloc or parts.fragment:
        return None, "not_same_origin_relative"
    if parts.path != info["detail_path"]:
        return None, "path_shape"
    query = parse_qsl(parts.query, keep_blank_values=True)
    if query != [("FilingId", str(filing_id))] or not str(filing_id).isdigit():
        return None, "filing_id_mismatch"
    return f"{info['origin']}{parts.path}?FilingId={filing_id}", None


def classify_documents(row, symbol):
    docs = []
    for pos, doc in enumerate(row.get("DocumentList") or []):
        kind = doc.get("DocumentType")
        role, sec_original, exts = DOCUMENT_ROLES.get(kind, ("unknown_type", False, ()))
        url = doc.get("Url") or ""
        ok, reason = check_cdn_url(url, symbol, exts) if url else (False, "empty_url")
        docs.append({"position": pos, "document_type": kind, "role": role, "url": url or None,
                     "fetchable": ok, "url_issue": reason,
                     # ORIG never carries a URL in the observed feeds: the SEC bytes are a gap.
                     "sec_original_bytes_available": bool(sec_original and ok),
                     "filing_document_id": doc.get("FilingDocumentId")})
    return docs


def plan_feed(data, symbol, *, source_sha256, through, forms=PLAN_FORMS):
    """Plan rows of one saved feed. Returns {"rows", "excluded", "stats"}; never ready."""
    info = issuer(symbol)
    if not isinstance(data, dict) or not isinstance(data.get("GetEdgarFilingListResult"), list):
        raise FeedError("missing GetEdgarFilingListResult")
    rows, excluded = [], []
    stats = Counter()
    for index, raw in enumerate(data["GetEdgarFilingListResult"]):
        stats["feed_rows"] += 1
        where = {"source_sha256": source_sha256, "row_index": index, "filing_id": raw.get("FilingId")}
        if raw.get("StockExchange") != "CIK" or raw.get("StockSymbol") != cik10(info["cik"]):
            stats["excluded_identity_mismatch"] += 1
            excluded.append(dict(where, reason="identity_mismatch", stock_exchange=raw.get("StockExchange"),
                                 stock_symbol=raw.get("StockSymbol"), form=raw.get("FilingTypeMnemonic")))
            continue
        form = raw.get("FilingTypeMnemonic")
        if form not in forms:
            stats["excluded_form"] += 1
            continue
        try:
            filing_date = parse_filing_date(raw.get("FilingDate"))
        except FeedError as err:
            stats["excluded_bad_date"] += 1
            excluded.append(dict(where, reason="bad_filing_date", detail=str(err), form=form))
            continue
        if filing_date > through:
            stats["excluded_after_through"] += 1
            continue
        missing = [f for f in FEED_FIELDS if f not in raw]
        detail, detail_issue = detail_url(raw.get("LinkToDetailPage"), symbol, raw.get("FilingId"))
        docs = classify_documents(raw, symbol)
        rows.append(dict(where, symbol=symbol, cik=info["cik"], form=form, filing_date=filing_date,
                         detail_url=detail, detail_url_issue=detail_issue, documents=docs,
                         missing_fields=missing, raw=raw,
                         # ReceivedDate/CreateDate are not verified publication instants.
                         available_at=None, ready=False))
        stats["planned"] += 1
        stats[f"form:{form}"] += 1
    stats.update(_doc_stats(rows))
    return {"symbol": symbol, "cik": info["cik"], "through": through, "source_sha256": source_sha256,
            "rows": rows, "excluded": excluded, "stats": dict(sorted(stats.items())), "ready": False}


def _doc_stats(rows):
    out = Counter()
    for row in rows:
        for d in row["documents"]:
            out[f"doc:{d['document_type']}:{'fetchable' if d['fetchable'] else d['url_issue']}"] += 1
    return out


def reference_rows(lines, cik, forms=PLAN_FORMS, through=None):
    out = []
    for rec in lines:
        if int(rec.get("cik", -1)) != int(cik) or rec.get("form") not in forms:
            continue
        if through and rec.get("filingDate", "") > through:
            continue
        out.append(rec)
    return out


def cross_check(plan, refs):
    """Group both sides by (form, filingDate). Only 1:1 groups yield an accession candidate."""
    q4 = defaultdict(list)
    for row in plan["rows"]:
        q4[(row["form"], row["filing_date"])].append(row)
    ref = defaultdict(list)
    for rec in refs:
        ref[(rec["form"], rec["filingDate"])].append(rec)
    matches, stats = [], Counter()
    for key in sorted(set(q4) | set(ref)):
        a, b = q4.get(key, []), ref.get(key, [])
        if len(a) == 1 and len(b) == 1:
            status = "unique_candidate"
        elif not b:
            status = "q4_only"
        elif not a:
            status = "reference_only"
        else:
            status = "ambiguous_same_day"
        stats[status] += 1
        stats[f"{status}:{key[0]}"] += 1
        accession = b[0]["accessionNumber"] if status == "unique_candidate" else None
        for row in a:
            matches.append({"filing_id": row["raw"].get("FilingId"), "row_index": row["row_index"],
                            "form": key[0], "filing_date": key[1], "status": status,
                            "accession_candidate": accession,
                            "match_basis": "issuer+form+filingDate unique in both" if accession else None,
                            "candidates": [r["accessionNumber"] for r in b] if status != "unique_candidate" else [],
                            "accession_verified": False})
        if not a:
            for rec in b:
                matches.append({"filing_id": None, "row_index": None, "form": key[0], "filing_date": key[1],
                                "status": status, "accession_candidate": None, "match_basis": None,
                                "candidates": [rec["accessionNumber"]], "accession_verified": False})
    return {"matches": matches, "stats": dict(sorted(stats.items())), "ready": False}


def detail_sec_links(html, symbol, row, refs):
    """Look for SEC archive links in a saved detail page; bind only under strict checks."""
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    found = sorted({(int(m.group(1)), m.group(2), m.group(0)) for m in _SEC_ARCHIVE.finditer(text)})
    cik = issuer(symbol)["cik"]
    out = {"filing_id": row["raw"].get("FilingId"), "sec_links": [u for _, _, u in found],
           "accession": None, "accession_bound": False, "basis": None, "issues": []}
    if not found:
        out["issues"].append("no_sec_archive_link_on_detail_page")
        return out
    accessions = {(c, a) for c, a, _ in found}
    if any(c != cik for c, _ in accessions):
        out["issues"].append("sec_link_cik_mismatch")
    own = sorted({a for c, a in accessions if c == cik})
    if len(own) != 1:
        out["issues"].append("not_exactly_one_accession")
        return out
    acc = f"{own[0][:10]}-{own[0][10:12]}-{own[0][12:]}"
    ref = [r for r in refs if r.get("accessionNumber") == acc]
    if len(ref) != 1 or ref[0]["form"] != row["form"] or ref[0]["filingDate"] != row["filing_date"]:
        out["issues"].append("reference_form_or_date_disagrees")
        return out
    if out["issues"]:
        return out
    out.update(accession=acc, accession_bound=True,
               basis="official detail page SEC archive link; CIK/form/filingDate agree with reference")
    return out
