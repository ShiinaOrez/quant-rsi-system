"""Offline parser for saved SEC EDGAR filing index pages (``{accession}-index.htm[l]``).

Input is the raw page bytes plus the caller's explicit original index URL, expected CIK and
accession, and a provenance mapping (capture URL, CDX digest, ...). Nothing is fetched and no
script runs; the stdlib HTMLParser only reads the markup. Output is a JSON-ready dict:

* form, Filing Date, Accepted, Period of Report and the declared Documents count, each with the
  raw text, page field and source line. Accepted is read only from the page's own "Accepted"
  field and interpreted as America/New_York wall time (aware, DST checked); a missing,
  unparseable, conflicting, DST-ambiguous or nonexistent value is an issue, never a guess from
  capture time, Last-Modified, a JSON "Z" or the filing date;
* every row of the "Document Format Files" and "Data Files" tables (sequence, description,
  document name, type, size, canonical original URL) plus the complete submission (SGML) link.
  Links must point at this filing's /Archives/edgar/data/{cik}/{accession}/ directory, directly
  or through a /ix?doc= wrapper whose single doc parameter is such a path. Other hosts,
  credentials, percent-encoding, backslashes, traversal, query strings or extra parameters are
  rejected; navigation links outside those tables are never treated as filing documents;
* Accepted is the SEC acceptance clock, not proof of public dissemination. This parser never
  binds available_at or declares PIT readiness; acceptance_metadata_ready only means the page
  passed its structural checks. Some accepted filings are disseminated the next business day;
* the declared count is the filing's document count, which can exceed the listed rows. Rows are
  only what the index lists (enumeration="index_listed_only"), never "all documents";
* derived files are marked only on explicit evidence (an "EXTRACTED" description or an EDGAR
  renderer file name such as R2.htm / Financial_Report.xlsx); other rows stay "not_marked" and are
  not claimed to be original submissions. All rows are kept for the caller to choose from.

Identity problems (bad URL, accession not the expected one on the URL, page or title) and pages
that are not an index at all (e.g. a rate-limit page without document tables or Accepted) raise
EdgarIndexError. Any blocking issue makes acceptance_metadata_ready False.
"""
import hashlib
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

SCHEMA = "edgar-filing-index.v1"
EASTERN = ZoneInfo("America/New_York")
MAX_BYTES = 4 * 1024 * 1024
TABLES = ("Document Format Files", "Data Files")
HEADER = ("Seq", "Description", "Document", "Type", "Size")
COMPLETE_SUBMISSION = "Complete submission text file"
BLOCKING, WARNING = "blocking", "warning"

_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ACCEPTED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_DIGITS = re.compile(r"[0-9]{1,9}")
_CIK = re.compile(r"[0-9]{1,10}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_RENDERER = re.compile(r"R[0-9]+\.(htm|xml|json)|Financial_Report\.xlsx|FilingSummary\.xml")
_URL_CHARS = re.compile(r"[!-~]+")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}
_FIELDS = {"Filing Date": "filing_date", "Accepted": "accepted", "Documents": "documents",
           "Period of Report": "period_of_report"}


class EdgarIndexError(ValueError):
    pass


def _text(parts):
    return " ".join("".join(parts).replace("\xa0", " ").split())


def _cik(value):
    text = str(value)
    if not _CIK.fullmatch(text) or int(text) == 0:
        raise EdgarIndexError(f"bad_expected_cik:{value!r}")
    return int(text)


def index_identity(url, expected_cik, expected_accession):
    """Check the original index URL against the expected CIK/accession; return its parts.

    The accession prefix is the submitter's id and may differ from the issuer CIK (filing
    agents), so only the URL path CIK is compared with expected_cik.
    """
    cik = _cik(expected_cik)
    if not isinstance(expected_accession, str) or not _ACCESSION.fullmatch(expected_accession):
        raise EdgarIndexError(f"bad_expected_accession:{expected_accession!r}")
    if not isinstance(url, str) or not _URL_CHARS.fullmatch(url) or "%" in url or "\\" in url:
        raise EdgarIndexError("index_url_bad_characters")
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc) != ("https", "www.sec.gov") or parts.query or parts.fragment:
        raise EdgarIndexError("index_url_not_sec_archives")
    segs = parts.path.split("/")
    nodash = expected_accession.replace("-", "")
    if len(segs) != 7 or segs[:4] != ["", "Archives", "edgar", "data"]:
        raise EdgarIndexError("index_url_not_sec_archives")
    if not _CIK.fullmatch(segs[4]) or int(segs[4]) != cik:
        raise EdgarIndexError(f"index_url_cik_mismatch:{segs[4]}")
    if segs[5] != nodash:
        raise EdgarIndexError(f"index_url_accession_mismatch:{segs[5]}")
    if segs[6] not in (f"{expected_accession}-index.htm", f"{expected_accession}-index.html"):
        raise EdgarIndexError(f"index_url_not_index_page:{segs[6]}")
    return {"cik": cik, "url_cik": segs[4], "accession": expected_accession,
            "accession_nodash": nodash, "folder": f"/Archives/edgar/data/{cik}/{nodash}/"}


def _archives_name(path, identity):
    segs = path.split("/")
    if len(segs) != 7 or segs[:4] != ["", "Archives", "edgar", "data"]:
        raise EdgarIndexError("link_not_archives_path")
    if not _CIK.fullmatch(segs[4]) or int(segs[4]) != identity["cik"]:
        raise EdgarIndexError("link_other_cik")
    if segs[5] != identity["accession_nodash"]:
        raise EdgarIndexError("link_other_accession")
    if not _FILENAME.fullmatch(segs[6]):
        raise EdgarIndexError("link_bad_filename")
    return segs[6]


def resolve_document_link(href, identity):
    """Validate a document-table href; return (canonical original URL, file name, link kind)."""
    if not href or not _URL_CHARS.fullmatch(href):
        raise EdgarIndexError("link_bad_characters")
    if "%" in href or "\\" in href or "@" in href:
        raise EdgarIndexError("link_encoded_or_credentials")
    parts = urlsplit(href)
    if parts.scheme not in ("", "https") or parts.netloc not in ("", "www.sec.gov"):
        raise EdgarIndexError("link_other_host")
    if parts.scheme and not parts.netloc:
        raise EdgarIndexError("link_other_host")
    if parts.fragment or "#" in href:
        raise EdgarIndexError("link_fragment")
    if not parts.path.startswith("/"):
        raise EdgarIndexError("link_not_root_relative")
    if parts.path == "/ix":
        try:
            params = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise EdgarIndexError("ix_query_malformed") from None
        if len(params) != 1 or params[0][0] != "doc":
            raise EdgarIndexError("ix_query_not_single_doc")
        doc = params[0][1]
        if not doc.startswith("/") or not _URL_CHARS.fullmatch(doc):
            raise EdgarIndexError("ix_doc_not_archives_path")
        name, kind = _archives_name(doc, identity), "ix_wrapper"
    else:
        if parts.query or "?" in href:
            raise EdgarIndexError("link_query")
        name, kind = _archives_name(parts.path, identity), "direct"
    return f"https://www.sec.gov{identity['folder']}{name}", name, kind


def eastern_accepted(raw):
    """Interpret an index-page Accepted value as America/New_York wall time.

    Returns (aware datetime or None, issue code or None). Fall-back hours (fold ambiguity) and
    spring-forward gaps are refused rather than resolved.
    """
    if not _ACCEPTED.fullmatch(raw):
        return None, "accepted_unparseable"
    try:
        naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, "accepted_unparseable"
    early, late = naive.replace(tzinfo=EASTERN, fold=0), naive.replace(tzinfo=EASTERN, fold=1)
    if early.utcoffset() != late.utcoffset():
        roundtrip = early.astimezone(timezone.utc).astimezone(EASTERN).replace(tzinfo=None)
        return None, ("accepted_ambiguous_dst_fold" if roundtrip == naive
                      else "accepted_nonexistent_local_time")
    return early, None


class _Page(HTMLParser):
    """Collects the index-page elements this module reads; tolerant of missing end tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.titles, self.form_names, self.sec_nums = [], [], []
        self.fields, self.companies, self.ident = [], [], []
        self.tables, self.issues, self.comments = [], [], []
        self.pending_head = self.last_p = self.table = self.row = self.cell = None
        self.html_closed = False

    def issue(self, code, severity, detail=""):
        self.issues.append({"code": code, "severity": severity, "detail": detail,
                            "line": self.getpos()[0]})

    def _inside(self, kind):
        return any(entry["kind"] == kind for entry in self.stack)

    def _close_to(self, tags, stop="table"):
        while self.stack and self.stack[-1]["tag"] in tags and self.stack[-1]["tag"] != stop:
            self._finish(self.stack.pop())

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        if tag == "tr" and self.table is not None:
            self._close_to({"td", "th", "tr", "a", "span", "acronym", "b", "strong"})
        elif tag in ("td", "th") and self.row is not None:
            self._close_to({"td", "th", "a", "span", "acronym", "b", "strong"})
        attr = dict(attrs)
        classes, ident = (attr.get("class") or "").split(), attr.get("id")
        kind, payload = None, None
        if tag == "title":
            kind = "title"
        elif tag == "div" and ident == "formName":
            kind = "formName"
        elif tag == "strong" and self._inside("formName"):
            kind = "formStrong"
        elif tag == "div" and ident == "secNum":
            kind = "secNum"
        elif tag == "div" and "infoHead" in classes:
            kind = "infoHead"
        elif tag == "div" and "info" in classes:
            kind = "info"
        elif tag == "span" and "companyName" in classes and not self._inside("companyName"):
            kind = "companyName"
        elif tag == "p" and "identInfo" in classes:
            kind = "identInfo"
        elif tag == "p" and self.table is None:
            kind = "p"
        elif tag == "table":
            if self.table is not None:
                self.issue("nested_table", BLOCKING)
            if "tableFile" in classes:
                kind = "table"
                self.table = {"summary": attr.get("summary"), "heading": self.last_p,
                              "header": None, "rows": [], "closed": False,
                              "line": self.getpos()[0], "dropped_rows": 0}
        elif tag == "tr" and self.table is not None:
            kind, self.row = "tr", {"cells": [], "line": self.getpos()[0]}
        elif tag in ("td", "th") and self.row is not None:
            kind = "cell"
            self.cell = {"tag": tag, "anchors": []}
        elif tag == "a" and self.cell is not None:
            kind = "a"
            payload = {"href": attr.get("href"), "parts": [],
                       "href_count": sum(1 for key, _ in attrs if key == "href")}
            self.cell["anchors"].append(payload)
        self.stack.append({"tag": tag, "kind": kind, "parts": [] if kind else None,
                           "payload": payload, "line": self.getpos()[0]})

    def handle_endtag(self, tag):
        if tag == "html":
            self.html_closed = True
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                while len(self.stack) > index:
                    self._finish(self.stack.pop())
                return

    def handle_data(self, data):
        for entry in self.stack:
            if entry["parts"] is not None:
                entry["parts"].append(data)
        for entry in self.stack:
            if entry["payload"] is not None:
                entry["payload"]["parts"].append(data)

    def handle_comment(self, data):
        self.comments.append(" ".join(data.split()))

    def _finish(self, entry):
        kind = entry["kind"]
        if kind is None:
            return
        text, line = _text(entry["parts"]), entry["line"]
        if kind == "title":
            self.titles.append((text, line))
        elif kind == "formStrong":
            self.form_names.append((text, line))
        elif kind == "secNum":
            self.sec_nums.append((text, line))
        elif kind == "infoHead":
            if self.pending_head is not None:
                self.issue("info_head_without_value", BLOCKING, self.pending_head[0])
            self.pending_head = (text, line)
        elif kind == "info":
            if self.pending_head is None:
                self.issue("info_value_without_head", BLOCKING, text)
            else:
                self.fields.append((self.pending_head[0], text, line))
                self.pending_head = None
        elif kind == "companyName":
            self.companies.append((text, line))
        elif kind == "identInfo":
            self.ident.append((text, line))
        elif kind == "p":
            self.last_p = text
        elif kind == "table":
            self.table["closed"] = True
            self.tables.append(self.table)
            self.table = self.row = self.cell = None
            self.last_p = None
        elif kind == "tr" and self.row is not None:
            cells = self.row["cells"]
            if cells and all(cell["tag"] == "th" for cell in cells):
                if self.table["header"] is not None:
                    self.issue("repeated_table_header", BLOCKING)
                self.table["header"] = tuple(cell["text"] for cell in cells)
            elif cells:
                self.table["rows"].append(self.row)
            self.row = None
        elif kind == "cell" and self.cell is not None and self.row is not None:
            self.cell["text"] = text
            self.row["cells"].append(self.cell)
            self.cell = None
        elif kind == "a":
            entry["payload"]["text"] = _text(entry["payload"]["parts"])

    def finish(self):
        self.close()
        if self.table is not None:
            if self.row is not None:
                self.table["dropped_rows"] += 1
            self.tables.append(self.table)
            self.table = None
        if self.pending_head is not None:
            self.issue("info_head_without_value", BLOCKING, self.pending_head[0])


def _sourced(value, raw, field, line):
    return {"value": value, "raw": raw, "field": field, "line": line}


def _single(page, label, issues):
    """The one value of an info field; duplicates that disagree are a blocking conflict."""
    found = [(value, line) for name, value, line in page.fields if name == label]
    if not found:
        return None
    if len({value for value, _ in found}) > 1:
        issues.append({"code": "conflicting_field", "severity": BLOCKING, "line": found[1][1],
                       "detail": f"{label}: " + " | ".join(value for value, _ in found)})
        return None
    if len(found) > 1:
        issues.append({"code": "duplicate_field", "severity": WARNING, "line": found[1][1],
                       "detail": label})
    return found[0]


def _iso_date(found, label, issues):
    if found is None:
        return None
    raw, line = found
    try:
        if not _DATE.fullmatch(raw):
            raise ValueError
        value = date.fromisoformat(raw)
    except ValueError:
        issues.append({"code": "bad_date", "severity": BLOCKING, "line": line,
                       "detail": f"{label}: {raw!r}"})
        return None
    return _sourced(value.isoformat(), raw, f"infoHead:{label}", line)


def _classify(form, doc_type, description, name):
    upper = doc_type.upper()
    if "EXTRACTED" in description.upper().split():
        derivation = ("derived", "description_EXTRACTED")
    elif _RENDERER.fullmatch(name):
        derivation = ("derived", "edgar_renderer_filename")
    else:
        derivation = ("not_marked", None)
    if derivation[0] == "derived":
        role = "derived"
    elif form and upper == form.upper():
        role = "primary"
    elif upper.startswith("EX-101."):
        role = "xbrl_schema_or_linkbase"
    elif upper.startswith("EX-"):
        role = "exhibit"
    elif upper == "GRAPHIC":
        role = "graphic"
    else:
        role = "other"
    return role, derivation


def _document(table_name, index, row, identity, form, issues):
    line = row["line"]
    cells = row["cells"]

    def bad(code, detail=""):
        issues.append({"code": code, "severity": BLOCKING, "line": line,
                       "detail": f"{table_name}#{index}: {detail}".rstrip(": ")})

    if len(cells) != len(HEADER):
        bad("malformed_row", f"{len(cells)} cells")
        return None
    seq_raw, description, doc_text, doc_type, size_raw = (cell["text"] for cell in cells)
    sequence = None
    if seq_raw:
        if _DIGITS.fullmatch(seq_raw) and int(seq_raw) > 0:
            sequence = int(seq_raw)
        else:
            bad("bad_sequence", seq_raw)
    size = None
    if size_raw:
        if _DIGITS.fullmatch(size_raw):
            size = int(size_raw)
        else:
            bad("bad_size", size_raw)
    anchors = cells[2]["anchors"]
    href = name = url = link_kind = link_error = None
    if len(anchors) != 1:
        bad("document_link_count", str(len(anchors)))
    elif anchors[0]["href_count"] != 1:
        bad("document_link_href_count", str(anchors[0]["href_count"]))
    else:
        href, name = anchors[0]["href"], anchors[0].get("text", "")
        try:
            url, url_name, link_kind = resolve_document_link(href, identity)
        except EdgarIndexError as exc:
            link_error = str(exc)
            bad("rejected_link", f"{link_error} {href!r}")
        else:
            if url_name != name:
                bad("link_name_mismatch", f"{name!r} -> {url_name!r}")
    markers = doc_text
    if name and markers.startswith(name):
        markers = markers[len(name):].strip()
    complete = description == COMPLETE_SUBMISSION
    if complete:
        role, derivation = "complete_submission", ("not_marked", None)
        if name != f"{identity['accession']}.txt" or sequence is not None:
            bad("complete_submission_mismatch", f"{name!r} seq={seq_raw!r}")
    else:
        role, derivation = _classify(form, doc_type, description, name or "")
        if sequence is None and not seq_raw:
            bad("missing_sequence", name or "")
    return {
        "table": table_name, "row": index, "line": line,
        "sequence": sequence, "sequence_raw": seq_raw, "description": description,
        "name": name, "document_cell_text": doc_text, "cell_markers": markers,
        "inline_xbrl_marker": "iXBRL" in markers.split(),
        "type": doc_type, "size": size, "size_raw": size_raw,
        "size_status": "known" if size is not None else ("unknown" if not size_raw else "invalid"),
        "href_raw": href, "link_kind": link_kind, "link_error": link_error, "original_url": url,
        "role": role, "derivation": derivation[0], "derivation_marker": derivation[1],
    }


def _check_rows(documents, declared, issues):
    def add(code, severity, detail):
        issues.append({"code": code, "severity": severity, "line": None, "detail": detail})

    def key(doc):
        return (doc["sequence"], doc["description"], doc["name"], doc["type"], doc["size_raw"],
                doc["original_url"])

    by_seq, by_name = {}, {}
    for doc in documents:
        if doc["sequence"] is not None:
            by_seq.setdefault(doc["sequence"], []).append(doc)
        if doc["name"]:
            by_name.setdefault(doc["name"], []).append(doc)
    for seq, group in sorted(by_seq.items()):
        if len(group) > 1:
            same = len({key(doc) for doc in group}) == 1
            add("duplicate_row" if same else "conflicting_sequence", WARNING if same else BLOCKING,
                f"seq {seq}: " + ", ".join(f"{d['table']}#{d['row']}" for d in group))
    for name, group in sorted(by_name.items()):
        if len({key(doc) for doc in group}) > 1 and len({d["sequence"] for d in group}) > 1:
            add("conflicting_document_name", BLOCKING,
                f"{name}: sequences {sorted(str(d['sequence']) for d in group)}")
    complete = [doc for doc in documents if doc["role"] == "complete_submission"]
    if not complete:
        add("complete_submission_missing", BLOCKING, "no complete submission text file row")
    elif len({key(doc) for doc in complete}) > 1:
        add("conflicting_complete_submission", BLOCKING, f"{len(complete)} rows")
    elif len(complete) > 1:
        add("duplicate_row", WARNING, "complete submission listed more than once")
    primary = [doc for doc in documents if doc["role"] == "primary"]
    if not primary:
        add("primary_document_missing", WARNING, "no row whose type equals the form")
    elif len({doc["name"] for doc in primary}) > 1:
        add("multiple_primary_candidates", WARNING, ", ".join(d["name"] or "" for d in primary))
    if declared is not None:
        if len(by_seq) > declared:
            add("listed_exceeds_declared", BLOCKING, f"{len(by_seq)} sequences > {declared}")
        elif len(by_seq) < declared:
            add("declared_count_exceeds_listed", WARNING,
                f"declared {declared}, index lists {len(by_seq)} sequenced rows")
        if by_seq and max(by_seq) > declared:
            add("sequence_exceeds_declared", WARNING, f"max {max(by_seq)} > {declared}")


def parse_edgar_index(raw, *, original_index_url, expected_cik, expected_accession, provenance,
                      max_bytes=MAX_BYTES):
    """Parse one saved filing index page. See the module docstring for the contract."""
    if not isinstance(raw, (bytes, bytearray)):
        raise EdgarIndexError("raw_not_bytes")
    if len(raw) > max_bytes:
        raise EdgarIndexError(f"raw_too_large:{len(raw)}")
    if (not isinstance(provenance, Mapping) or not provenance
            or not all(isinstance(k, str) and isinstance(v, str) and k and v
                       for k, v in provenance.items())):
        raise EdgarIndexError("provenance_required")
    identity = index_identity(original_index_url, expected_cik, expected_accession)
    raw = bytes(raw)
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EdgarIndexError(f"not_utf8:{exc.start}") from None
    page = _Page()
    page.feed(html)
    page.finish()
    issues = list(page.issues)

    def add(code, severity, detail="", line=None):
        issues.append({"code": code, "severity": severity, "detail": detail, "line": line})

    known = [t for t in page.tables if (t["summary"] or t["heading"]) in TABLES]
    accepted_found = [f for f in page.fields if f[0] == "Accepted"]
    if not known and not accepted_found:
        raise EdgarIndexError("not_an_edgar_filing_index")

    acc = identity["accession"]
    for text, line in page.titles:
        found = set(_ACCESSION.findall(text))
        if found and found != {acc}:
            raise EdgarIndexError(f"title_accession_mismatch:{sorted(found)}")
    page_acc = None
    for text, line in page.sec_nums:
        found = set(_ACCESSION.findall(text))
        if found != {acc}:
            raise EdgarIndexError(f"page_accession_mismatch:{sorted(found)}")
        page_acc = _sourced(acc, text, "secNum", line)
    if page_acc is None:
        add("page_accession_missing", BLOCKING)

    form = None
    forms = {}
    for text, line in page.form_names:
        match = re.fullmatch(r"Form (\S+)", text)
        if match:
            forms.setdefault(match.group(1), (text, line))
        else:
            add("unparsed_form_name", BLOCKING, text, line)
    if len(forms) == 1:
        value, (text, line) = next(iter(forms.items()))
        form = _sourced(value, text, "formName", line)
    elif forms:
        add("conflicting_field", BLOCKING, "Form: " + " | ".join(forms), None)
    else:
        add("form_missing", BLOCKING)

    filers = []
    for text, line in page.companies:
        match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)\s*CIK\s*:\s*([0-9]{10})\b.*", text)
        if match:
            filers.append({"name": match.group(1), "role": match.group(2),
                           "cik": int(match.group(3)), "raw": text, "line": line})
        else:
            add("unparsed_company", WARNING, text, line)
    if not filers:
        add("filer_info_missing", BLOCKING)
    elif identity["cik"] not in {f["cik"] for f in filers}:
        add("expected_cik_not_on_page", BLOCKING, str(sorted({f["cik"] for f in filers})))
    for text, line in page.ident:
        match = re.search(r"\bType:\s*([^|]+?)\s*(\||$)", text)
        if match and form and match.group(1) != form["value"]:
            add("form_type_conflict", BLOCKING, f"{form['value']} vs Type: {match.group(1)}", line)

    filing_date = _iso_date(_single(page, "Filing Date", issues), "Filing Date", issues)
    period = _iso_date(_single(page, "Period of Report", issues), "Period of Report", issues)
    if filing_date is None and not any(f[0] == "Filing Date" for f in page.fields):
        add("filing_date_missing", BLOCKING)
    if period is None and not any(f[0] == "Period of Report" for f in page.fields):
        add("period_of_report_missing", WARNING)

    accepted = None
    found = _single(page, "Accepted", issues)
    if found is not None:
        value, problem = eastern_accepted(found[0])
        if problem:
            add(problem, BLOCKING, found[0], found[1])
        else:
            accepted = {**_sourced(value.isoformat(), found[0], "infoHead:Accepted", found[1]),
                        "timezone": "America/New_York", "utc": value.astimezone(timezone.utc)
                        .isoformat().replace("+00:00", "Z"), "tz_rule": "edgar_eastern_wall_time"}
            if filing_date and date.fromisoformat(filing_date["value"]) < value.date():
                add("filing_date_before_accepted", BLOCKING,
                    f"{filing_date['value']} < {value.date()}")
    elif not accepted_found:
        add("accepted_missing", BLOCKING)

    declared = None
    found = _single(page, "Documents", issues)
    if found is not None:
        if _DIGITS.fullmatch(found[0]):
            declared = _sourced(int(found[0]), found[0], "infoHead:Documents", found[1])
        else:
            add("bad_document_count", BLOCKING, found[0], found[1])
    elif not any(f[0] == "Documents" for f in page.fields):
        add("document_count_missing", WARNING)
    other_fields = [{"field": name, "raw": value, "line": line}
                    for name, value, line in page.fields if name not in _FIELDS]

    documents, seen_tables = [], set()
    for table in page.tables:
        summary, heading = table["summary"], table["heading"]
        name = summary or heading
        if summary and heading and summary != heading:
            add("table_label_conflict", BLOCKING, f"{summary!r} vs {heading!r}", table["line"])
            continue
        if name not in TABLES:
            add("unrecognized_table", BLOCKING, repr(name), table["line"])
            continue
        if name in seen_tables:
            add("duplicate_table", BLOCKING, name, table["line"])
        seen_tables.add(name)
        if not table["closed"]:
            add("unterminated_table", BLOCKING, f"{name}; partial row dropped: "
                f"{table['dropped_rows']}", table["line"])
        if table["header"] != HEADER:
            add("unexpected_table_header", BLOCKING, repr(table["header"]), table["line"])
            continue
        for index, row in enumerate(table["rows"], 1):
            doc = _document(name, index, row, identity, form and form["value"], issues)
            if doc is not None:
                documents.append(doc)
    if "Document Format Files" not in seen_tables:
        add("document_format_table_missing", BLOCKING)
    _check_rows(documents, declared and declared["value"], issues)

    for marker in ("END DOCUMENT DIV", "END FILER DIV"):
        if marker not in page.comments:
            add("layout_end_marker_missing", BLOCKING, marker)
    if not page.html_closed:
        add("no_html_close_tag", WARNING, "page ends without </html>; content markers checked")

    complete = next((d for d in documents if d["role"] == "complete_submission"), None)
    listed = [d for d in documents if d["role"] != "complete_submission"]
    blocking = any(i["severity"] == BLOCKING for i in issues)
    acceptance_metadata_ready = accepted is not None and not blocking
    return {
        "schema": SCHEMA,
        "source": {"original_index_url": original_index_url, "raw_sha256":
                   hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw),
                   "provenance": dict(provenance)},
        "identity": {"expected_cik": identity["cik"], "expected_accession": acc,
                     "url_cik": identity["url_cik"], "url_accession": identity["accession_nodash"],
                     "page_accession": page_acc, "filers": filers,
                     "accession_prefix_is_expected_cik": int(acc[:10]) == identity["cik"]},
        "form": form, "filing_date": filing_date, "accepted": accepted,
        "period_of_report": period, "declared_document_count": declared,
        "other_fields": other_fields,
        "documents": documents,
        "complete_submission_url": complete and complete["original_url"],
        "counts": {"listed_rows": len(listed),
                   "listed_sequences": len({d["sequence"] for d in listed if d["sequence"]}),
                   "declared": declared and declared["value"],
                   "by_table": {t: sum(d["table"] == t for d in documents) for t in TABLES}},
        "enumeration": "index_listed_only",
        "issues": issues,
        "acceptance_metadata_ready": acceptance_metadata_ready,
        "pit_ready": False,
        "available_at": None,
        "availability_status": "public_dissemination_not_verified",
        "benchmark_ready": False,
    }
