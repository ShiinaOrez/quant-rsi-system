"""Resumable collector for issuer-hosted SEC filing mirrors (currently INTC only).

Reuses archive_sources.Store (content-addressed blobs, append-only fetch log
and catalog) and archive_sources.Fetcher (retries, 403 breaker, cache).  All
hosts share one request slot, so the collector never issues more than one
request per second.

The mirror is a secondary copy of EDGAR served by the issuer's IR vendor.  It
gives filing dates (MM/DD/YY) and documents, but no SGML header, so accepted_at
stays null and is reported as a gap.  Nothing here claims the mirror lists
every SEC filing; completeness is only judged against the mirror's own pager.

Commands (all take --root and --symbol):
  plan      fetch the landing page, then walk every page of each plan form via
            the pager's own Next links; write index.json + queue.jsonl
  fetch     download pending queue items, --limit N per run, resumable
  coverage  per-form pager completeness, filings, files fetched/failed/pending
"""
import argparse
import importlib.util
import io
import json
import math
import re
import sys
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_spec = importlib.util.spec_from_file_location("archive_sources", Path(__file__).with_name("archive_sources.py"))
arch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arch)

# Latest terminal session of the current suite (INTC legacy task: 60 sessions after 2024-07-11).
# Filings dated after this are indexed but not downloaded.
DEFAULT_HORIZON = "2024-10-04"
IMAGE_EXT = (".jpg", ".jpeg", ".gif", ".png", ".bmp", ".tif", ".tiff")
ACCESSION_RE = re.compile(r"/content/(\d{10}-\d{2}-\d{6})/")
ROLE_ORDER = {"main": 0, "attachment": 1, "xbrl_zip": 2}


class Mirror:
    """One issuer mirror: landing URL, allowed hosts and the issuer it is assumed to serve."""

    def __init__(self, symbol, landing_url, pinned_cik, hosts, task_windows):
        self.symbol, self.landing_url, self.pinned_cik = symbol, landing_url, pinned_cik
        self.hosts = set(hosts)
        # (from, to) filing-date windows that matter for the suite's tasks; used for queue priority only.
        self.task_windows = task_windows


# Intel's IR site (spr-* markup). Structure verified on saved pages in
# artifacts/benchmark-quality/issuer-mirrors/INTC*.html.
MIRRORS = {"INTC": Mirror("INTC", "https://www.intc.com/filings-reports/all-sec-filings", 50863,
                          ["www.intc.com"], [("2023-07-01", DEFAULT_HORIZON)])}


class MirrorFetcher(arch.Fetcher):
    """All hosts share a single slot: at most one request per interval overall."""

    def _wait(self, host):
        super()._wait("*")


def make_fetcher(store, timeout=20.0, retries=2, interval=1.0, **kw):
    if not math.isfinite(interval) or interval < 1:
        raise ValueError("mirror interval must be at least one second")
    return MirrorFetcher(store, timeout=timeout, retries=retries, max_consecutive_403=1,
                         intervals={"*": interval}, **kw)


# ---------------------------------------------------------------- parsing

class IndexParser(HTMLParser):
    """Extracts the filter form, the filings table rows and the pager from one index page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.base, self.forms, self.selects, self.rows, self.pager = None, [], {}, [], []
        self._select = self._option = self._row = self._cell = self._anchor = None
        self._in_table = self._in_tbody = self._in_pager = False
        self._li_class, self._divs = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "form":
            self.forms.append({"action": a.get("action"), "method": (a.get("method") or "get").lower(),
                               "id": a.get("id")})
        elif tag == "select":
            self._select = a.get("name")
            self.selects[self._select] = []
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "selected": "selected" in a, "text": ""}
            self.selects[self._select].append(self._option)
        elif tag == "table" and "spr-ir-sec-filings" in cls:
            self._in_table = True
        elif tag == "tbody" and self._in_table:
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row = {"cells": {}, "anchors": []}
        elif tag == "td" and self._row is not None:
            self._cell = cls[0] if cls else ""
            self._row["cells"].setdefault(self._cell, "")
        elif tag == "div":
            self._divs.append("spr-expandable" in cls)
        elif tag == "ul" and "pagination" in cls:
            self._in_pager = True
        elif tag == "li" and self._in_pager:
            self._li_class = cls
        elif tag == "a":
            self._anchor = {"href": a.get("href"), "title": a.get("title"), "class": cls, "text": ""}
            if self._row is not None and self._cell is not None:
                self._anchor.update(cell=self._cell, in_expandable=any(self._divs),
                                    fancybox=a.get("data-fancybox"))
                self._row["anchors"].append(self._anchor)
            elif self._in_pager:
                self._anchor["li_class"] = self._li_class
                self.pager.append(self._anchor)

    def handle_endtag(self, tag):
        if tag == "option":
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"].split())
            self._anchor = None
        elif tag == "td":
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "table":
            self._in_table = self._in_tbody = False
        elif tag == "div" and self._divs:
            self._divs.pop()
        elif tag == "ul":
            self._in_pager = False

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data
        if self._anchor is not None:
            self._anchor["text"] += data
        elif self._row is not None and self._cell is not None:
            self._row["cells"][self._cell] += data


def parse_index_page(data, page_url):
    p = IndexParser()
    p.feed(data.decode("utf-8", errors="replace"))
    p.close()
    base = p.base or page_url
    options = {name: [(o["value"] if o["value"] is not None else o["text"]).strip() for o in opts]
               for name, opts in p.selects.items()}
    pages, next_url, active = {}, None, None
    for a in p.pager:
        url = urllib.parse.urljoin(base, a["href"] or "")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
        n = q.get("page", [""])[0]
        n = int(n) if n.isdigit() else None
        if a["text"] == "Next":
            next_url = url
        elif n is not None and a["text"] != "Previous":
            pages.setdefault(n, url)
        if "active" in a["li_class"]:
            active = n
    return {"base": base, "forms": p.forms, "options": options, "rows": p.rows,
            "pager": {"active": active, "last_page": max(pages) if pages else None, "next_url": next_url,
                      "links": {str(k): v for k, v in sorted(pages.items())}}}


def resolve_date(text, offered_years):
    """MM/DD/YY -> ISO date; century resolved only through the mirror's own year list."""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", text.strip())
    if not m:
        return None
    years = [y for y in offered_years if y % 100 == int(m.group(3))]
    if len(years) != 1:
        return None
    try:
        return date(years[0], int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def normalize_row(row, base, offered_years, requested_form):
    """One table row -> filing record plus row-level findings."""
    findings, anchors = [], row["anchors"]

    def absolute(a):
        return urllib.parse.urljoin(base, a["href"] or "")

    form_links = [a for a in anchors if a["cell"] == "spr-sec-form" and a["href"]]
    attach = [a for a in anchors if a["cell"] == "spr-sec-desc" and a["in_expandable"] and a["href"]]
    titles = [a for a in anchors if a["cell"] == "spr-sec-desc" and not a["in_expandable"]]
    zips = [a for a in anchors if a["title"] == "Download Raw XBRL Files"]
    inventory_only = [{"role": "mirror_pdf" if a["cell"] == "spr-sec-view-pdf" else "derived_" + (a["title"] or "link")
                       .lower().replace(" ", "_"), "url": absolute(a)}
                      for a in anchors if a["cell"] in ("spr-sec-view-pdf", "spr-sec-view-docs") and a not in zips]
    date_text = " ".join(row["cells"].get("spr-sec-date", "").split())
    rec = {"date_text": date_text, "filing_date": resolve_date(date_text, offered_years),
           "form": form_links[0]["text"] if form_links else None, "requested_form": requested_form,
           "description": titles[0]["text"] if titles else None,
           "pages": " ".join(row["cells"].get("spr-sec-pages", "").split()) or None,
           "accepted_at": None, "accepted_at_source": None, "files": [], "inventory_only": inventory_only}
    if not form_links:
        findings.append({"check": "row_without_main_document", "severity": "error", "date_text": date_text})
    if rec["filing_date"] is None:
        findings.append({"check": "filing_date_unparseable", "severity": "error", "date_text": date_text})
    seen = set()
    for role, a in [("main", x) for x in form_links[:1]] + [("attachment", x) for x in attach] + \
                   [("xbrl_zip", x) for x in zips]:
        url = absolute(a)
        if url in seen:
            continue
        seen.add(url)
        rec["files"].append({"role": role, "label": a["text"] or a["title"], "url": url,
                             "filename": urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])})
    accs = {m.group(1) for f in rec["files"] for m in [ACCESSION_RE.search(f["url"])] if m}
    accs |= {m.group(0) for a in anchors if a.get("fancybox")
             for m in [re.search(r"\d{10}-\d{2}-\d{6}", a["fancybox"])] if m}
    if len(accs) != 1:
        findings.append({"check": "row_accession_missing_or_mixed", "severity": "error", "accessions": sorted(accs),
                         "date_text": date_text})
    rec["accession"] = accs.pop() if len(accs) == 1 else None
    if rec["form"] is not None and rec["form"] != requested_form:
        findings.append({"check": "form_mismatch_vs_request", "severity": "error", "requested": requested_form,
                         "returned": rec["form"], "accession": rec["accession"]})
    if len(form_links) > 1 and len({absolute(a) for a in form_links}) > 1:
        findings.append({"check": "multiple_main_documents", "severity": "error", "accession": rec["accession"]})
    return rec, findings


# ---------------------------------------------------------------- plan

def _form_url(landing, form):
    """First page of one form, built from the page's own filter form (GET, its select names)."""
    spec = next((f for f in landing["forms"] if f["id"] == "sec-filing-filter"), None)
    if spec is None or spec["method"] != "get" or "form_type" not in landing["options"]:
        raise ValueError("filter form not found on landing page; mirror markup changed")
    action = urllib.parse.urljoin(landing["base"], spec["action"])
    fields = {name: (form if name == "form_type" else "") for name in landing["options"]}
    return action + "?" + urllib.parse.urlencode(fields)


def _page_record(rec):
    return {"url": rec["url"], "status": rec.get("status"), "sha256": rec.get("sha256"),
            "retrieved_at": rec.get("retrieved_at") or rec.get("at"), "cache_hit": rec.get("cache_hit")}


def traverse_form(fetcher, symbol, landing, form, offered_years, max_pages=None, refresh=False):
    """Follow the pager's Next links for one form; every stop reason is reported."""
    out = {"form": form, "pages": [], "rows": [], "findings": [], "last_page": None, "truncated": False,
           "stop_reason": None}
    url, page_no = _form_url(landing, form), 1
    per_page = None
    while url:
        if max_pages is not None and page_no > max_pages:
            out.update(truncated=True, stop_reason=f"max_pages={max_pages}")
            break
        try:
            rec = fetcher.get(f"mirror:{symbol}:index:{form}:page:{page_no}", url, refresh=refresh,
                              meta={"symbol": symbol, "form": form, "page": page_no})
        except arch.HostBlocked as host:
            out.update(truncated=True, stop_reason=f"host_blocked:{host}")
            break
        out["pages"].append(dict(_page_record(rec), page=page_no))
        if not rec["ok"]:
            out.update(truncated=True, stop_reason=f"http_{rec.get('status')}:{rec.get('error')}")
            break
        parsed = parse_index_page(fetcher.store.read(rec["sha256"]), url)
        pager = parsed["pager"]
        if out["last_page"] is None or (pager["last_page"] or page_no) > out["last_page"]:
            out["last_page"] = pager["last_page"] or page_no
        if pager["active"] not in (None, page_no):
            out["findings"].append({"check": "pager_active_page_mismatch", "severity": "error", "page": page_no,
                                    "active": pager["active"]})
        out["pages"][-1]["rows"] = len(parsed["rows"])
        for raw in parsed["rows"]:
            row, findings = normalize_row(raw, parsed["base"], offered_years, form)
            row["source_page"] = {"form": form, "page": page_no, "sha256": rec["sha256"]}
            out["rows"].append(row)
            out["findings"] += [dict(f, page=page_no) for f in findings]
        if per_page is None:
            per_page = len(parsed["rows"])
        nxt = pager["next_url"]
        if nxt is None:
            if page_no < out["last_page"]:
                out.update(truncated=True, stop_reason="next_link_missing_before_last_page")
            break
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(nxt).query, keep_blank_values=True)
        same_place = urllib.parse.urlsplit(nxt)[:3] == urllib.parse.urlsplit(url)[:3]
        if not same_place or q.get("form_type") != [form] or q.get("page") != [str(page_no + 1)]:
            out["findings"].append({"check": "next_link_unexpected", "severity": "error", "page": page_no,
                                    "next_url": nxt})
            out.update(truncated=True, stop_reason="next_link_unexpected")
            break
        url, page_no = nxt, page_no + 1
    for p in out["pages"][:-1]:
        if p.get("rows") is not None and p["rows"] != per_page:
            out["findings"].append({"check": "short_page_before_last", "severity": "error", "page": p["page"],
                                    "rows": p["rows"], "per_page": per_page})
    fetched = {p["page"] for p in out["pages"] if p["status"] == 200}
    out["pending_pages"] = [n for n in range(1, (out["last_page"] or 0) + 1) if n not in fetched]
    out["per_page"] = per_page
    return out


def _signature(row):
    return {"form": row["form"], "filing_date": row["filing_date"],
            "files": sorted(f["url"] for f in row["files"])}


def merge_rows(traversals, plan_forms):
    """Dedupe filings by accession across pages and forms; record identical repeats and conflicts."""
    filings, findings = {}, []
    for tr in traversals:
        for row in tr["rows"]:
            acc = row["accession"]
            if acc is None:
                continue
            if row["form"] not in plan_forms:
                findings.append({"check": "returned_form_outside_plan", "severity": "error", "accession": acc,
                                 "form": row["form"], "requested": row["requested_form"]})
                continue
            prev = filings.get(acc)
            if prev is None:
                filings[acc] = dict(row, seen_on=[row["source_page"]])
                continue
            prev["seen_on"].append(row["source_page"])
            diff = {k: [v, _signature(row)[k]] for k, v in _signature(prev).items() if v != _signature(row)[k]}
            same_form_traversal = prev["requested_form"] == row["requested_form"]
            if diff:
                findings.append({"check": "duplicate_conflict", "severity": "error", "accession": acc,
                                 "differences": diff, "pages": prev["seen_on"]})
            elif same_form_traversal:
                # The same filing on two pages of one traversal means the pager shifted under us.
                findings.append({"check": "duplicate_across_pages_pagination_shift", "severity": "error",
                                 "accession": acc, "pages": prev["seen_on"]})
            else:
                findings.append({"check": "duplicate_across_form_filters", "severity": "warning",
                                 "accession": acc, "pages": prev["seen_on"]})
    return sorted(filings.values(), key=lambda r: (r["filing_date"] or "", r["accession"]), reverse=True), findings


def _priority(mirror, filing, f):
    in_window = any(lo <= (filing["filing_date"] or "") <= hi for lo, hi in mirror.task_windows)
    is_image = f["filename"].lower().endswith(IMAGE_EXT)
    return (2 if is_image else 0 if in_window else 1)


def cmd_plan(fetcher, mirror, forms=arch.PLAN_FORMS, max_pages=None, horizon=DEFAULT_HORIZON, refresh=False):
    store, sym = fetcher.store, mirror.symbol
    out_dir = store.root / "mirror" / sym
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = fetcher.get(f"mirror:{sym}:landing", mirror.landing_url, refresh=refresh, meta={"symbol": sym})
    if not rec["ok"]:
        return {"symbol": sym, "error": f"landing_http_{rec.get('status')}", "landing": _page_record(rec)}
    landing = parse_index_page(store.read(rec["sha256"]), mirror.landing_url)
    offered_forms = [f for f in landing["options"].get("form_type", []) if f]
    offered_years = sorted(int(y) for y in landing["options"].get("year", []) if y.isdigit())
    traversals, not_offered = [], []
    for form in forms:
        if form not in offered_forms:
            not_offered.append(form)
            continue
        traversals.append(traverse_form(fetcher, sym, landing, form, offered_years, max_pages, refresh))
    filings, merge_findings = merge_rows(traversals, set(forms))
    queue, key_urls, key_findings = [], {}, []
    for filing in filings:
        filing["in_horizon"] = filing["filing_date"] is not None and filing["filing_date"] <= horizon
        if not filing["in_horizon"]:
            continue
        for f in filing["files"]:
            if urllib.parse.urlsplit(f["url"]).hostname not in mirror.hosts:
                key_findings.append({"check": "off_host_link_not_queued", "severity": "warning",
                                     "accession": filing["accession"], "url": f["url"]})
                continue
            key = f"mirror:{sym}:file:{filing['accession']}:{f['filename']}"
            if key in key_urls and key_urls[key] != f["url"]:
                key_findings.append({"check": "filename_collision", "severity": "error", "key": key,
                                     "urls": [key_urls[key], f["url"]]})
                continue
            key_urls[key] = f["url"]
            queue.append({"key": key, "symbol": sym, "accession": filing["accession"], "form": filing["form"],
                          "filing_date": filing["filing_date"], "accepted_at": None, "role": f["role"],
                          "label": f["label"], "filename": f["filename"], "url": f["url"],
                          "priority": _priority(mirror, filing, f), "source_page": filing["source_page"]})
    queue.sort(key=lambda i: (i["priority"], -int(i["filing_date"].replace("-", "")), i["accession"],
                              ROLE_ORDER[i["role"]], i["filename"]))
    index = {"symbol": sym, "mirror": mirror.landing_url, "pinned_cik": mirror.pinned_cik,
             "cik_verified_from_mirror": False, "planned_at": arch.now_utc(), "horizon": horizon,
             "max_pages": max_pages, "landing": _page_record(rec), "offered_forms": offered_forms,
             "offered_years": offered_years, "plan_forms": list(forms), "forms_not_offered": not_offered,
             "traversals": [{k: v for k, v in t.items() if k != "rows"} | {"rows": len(t["rows"])}
                            for t in traversals],
             "findings": [f for t in traversals for f in t["findings"]] + merge_findings + key_findings,
             "filings": filings}
    (out_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    (out_dir / "queue.jsonl").write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in queue))
    runs = out_dir / "plan-runs.jsonl"
    arch.Store.append(runs, {"at": index["planned_at"], "landing_sha256": rec["sha256"], "max_pages": max_pages,
                             "traversals": [{k: t[k] for k in ("form", "last_page", "truncated", "pending_pages",
                                                               "stop_reason")} for t in traversals],
                             "filings": len(filings), "queue": len(queue)})
    return plan_summary(index, queue)


def plan_summary(index, queue):
    by_form = {}
    for f in index["filings"]:
        e = by_form.setdefault(f["form"], {"filings": 0, "in_horizon": 0})
        e["filings"] += 1
        e["in_horizon"] += f["in_horizon"]
    roles = {}
    for i in queue:
        k = "image" if i["filename"].lower().endswith(IMAGE_EXT) else i["role"]
        roles[k] = roles.get(k, 0) + 1
    return {"symbol": index["symbol"], "horizon": index["horizon"], "forms_not_offered": index["forms_not_offered"],
            "traversals": [{k: t[k] for k in ("form", "last_page", "rows", "truncated", "pending_pages",
                                              "stop_reason")} for t in index["traversals"]],
            "filings_by_form": by_form, "queue_files": len(queue), "queue_by_kind": roles,
            "findings": len(index["findings"]),
            "findings_by_check": _count(f["check"] for f in index["findings"])}


def _count(items):
    out = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


# ---------------------------------------------------------------- fetch

def load_queue(store, symbol):
    path = store.root / "mirror" / symbol / "queue.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def cmd_fetch(fetcher, symbol, limit, accessions=None, include_images=False, retry_failed=False, roles=None):
    store = fetcher.store
    catalog, failures = store.catalog(), store.last_failures()
    queue = load_queue(store, symbol)
    if accessions:
        order = {a: n for n, a in enumerate(accessions)}
        queue = sorted((i for i in queue if i["accession"] in order),
                       key=lambda i: (order[i["accession"]], ROLE_ORDER[i["role"]], i["filename"]))
    res = {"fetched": 0, "failed": 0, "skipped_cached": 0, "skipped_failed": 0, "skipped_images": 0,
           "blocked": False, "processed": 0, "items": []}
    for item in queue:
        if roles and item["role"] not in roles:
            continue
        if item["key"] in catalog and store.read(catalog[item["key"]]["sha256"]) is not None:
            res["skipped_cached"] += 1
            continue
        if not include_images and item["filename"].lower().endswith(IMAGE_EXT):
            res["skipped_images"] += 1
            continue
        if item["key"] in failures and not retry_failed and failures[item["key"]].get("status") in (403, 404, 410):
            res["skipped_failed"] += 1
            continue
        if res["processed"] >= limit:
            break
        res["processed"] += 1
        try:
            rec = fetcher.get(item["key"], item["url"],
                              meta={k: item[k] for k in ("symbol", "accession", "form", "filing_date", "role",
                                                         "label", "source_page")})
        except arch.HostBlocked:
            res["blocked"] = True
            break
        res["fetched" if rec["ok"] else "failed"] += 1
        res["items"].append({"accession": item["accession"], "filename": item["filename"], "status": rec["status"],
                             "bytes": rec["bytes"], "sha256": rec.get("sha256"), "error": rec.get("error")})
    return res


def check_payload(item, data, content_type):
    """Cheap evidence that the bytes are the listed document, not an error shell."""
    problems, info = [], {}
    name = item["filename"].lower()
    if not data:
        return ["empty_body"], info
    if name.endswith(".zip"):
        if not data.startswith(b"PK\x03\x04"):
            problems.append("zip_magic_missing")
        else:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    members = zf.namelist()
                    if any(i.header_offset < 0 or i.header_offset >= len(data) for i in zf.infolist()):
                        problems.append("zip_invalid_member_offset")
                info["zip_members"] = len(members)
                info["zip_has_sgml_header"] = any(m.endswith(".txt") for m in members)
            except zipfile.BadZipFile:
                problems.append("zip_unreadable")
    elif name.endswith((".htm", ".html", ".txt", ".xml")):
        if content_type and not content_type.lower().startswith(("text/", "application/xhtml", "application/xml")):
            problems.append(f"unexpected_content_type:{content_type}")
        if b"spr-ir-sec-filings" in data:
            problems.append("index_page_returned_instead_of_document")
        if name.endswith((".htm", ".html")):
            if re.search(rb"<html(?:\s|>)", data, re.I) and not re.search(rb"</html\s*>", data, re.I):
                problems.append("html_end_tag_missing_possible_truncation")
    elif name.endswith(".pdf") and not data.startswith(b"%PDF"):
        problems.append("pdf_magic_missing")
    return problems, info


# ---------------------------------------------------------------- coverage

def cmd_coverage(store, mirror):
    sym = mirror.symbol
    path = store.root / "mirror" / sym / "index.json"
    if not path.exists():
        return {"symbol": sym, "benchmark_ready": False, "gaps": ["mirror_index_missing:run_plan"]}
    index = json.loads(path.read_text())
    queue = load_queue(store, sym)
    catalog, failures = store.catalog(), store.last_failures()
    gaps = ["accepted_at_unavailable_from_mirror:all_filings_null",
            "mirror_completeness_vs_sec_unverified",
            "mirror_bytes_not_verified_against_edgar_originals",
            "issuer_cik_not_stated_by_mirror:pinned_only"]
    gaps += [f"form_filter_not_offered_by_mirror:{f}" for f in index["forms_not_offered"]]
    forms = []
    for t in index["traversals"]:
        complete = (not t["truncated"] and not t["pending_pages"]
                    and not any(f["check"] in ("short_page_before_last", "pager_active_page_mismatch",
                                               "next_link_unexpected") for f in t["findings"]))
        shifted = [f for f in index["findings"] if f["check"] == "duplicate_across_pages_pagination_shift"
                   and any(p["form"] == t["form"] for p in f["pages"])]
        complete = complete and not shifted
        forms.append({"form": t["form"], "last_page": t["last_page"], "pages_fetched": len(t["pages"]),
                      "rows": t["rows"], "truncated": t["truncated"], "pending_pages": t["pending_pages"],
                      "stop_reason": t["stop_reason"], "pager_complete": complete})
        if not complete:
            gaps.append(f"pager_incomplete:{t['form']}:pending_pages={len(t['pending_pages'])}")
    errors = [f for f in index["findings"] if f["severity"] == "error"]
    if errors:
        gaps.append(f"index_findings_error:{len(errors)}")
    fetched, failed, pending, images_pending, blocked_pending, problems, sizes = [], [], [], 0, 0, [], {}
    for item in queue:
        rec = catalog.get(item["key"])
        data = store.read(rec["sha256"]) if rec else None
        kind = "image" if item["filename"].lower().endswith(IMAGE_EXT) else item["role"]
        if data is not None:
            issues, info = check_payload(item, data, rec.get("content_type"))
            fetched.append({"accession": item["accession"], "filename": item["filename"], "role": item["role"],
                            "filing_date": item["filing_date"], "sha256": rec["sha256"], "bytes": len(data),
                            "retrieved_at": rec["retrieved_at"], "url": item["url"], "accepted_at": None, **info})
            sizes.setdefault(kind, []).append(len(data))
            if issues:
                problems.append({"accession": item["accession"], "filename": item["filename"], "problems": issues})
        elif item["key"] in failures and failures[item["key"]].get("error") != "host_blocked_this_run":
            f = failures[item["key"]]
            failed.append({"accession": item["accession"], "filename": item["filename"], "role": item["role"],
                           "status": f.get("status"), "error": f.get("error"), "at": f.get("at")})
        else:
            pending.append(item)
            images_pending += kind == "image"
            blocked_pending += item["key"] in failures
    by_acc = {}
    for item in queue:
        by_acc.setdefault(item["accession"], []).append(item["key"])
    done_keys = {i["key"] for i in queue if catalog.get(i["key"]) and store.read(catalog[i["key"]]["sha256"])}
    filings_complete = sum(1 for keys in by_acc.values() if all(k in done_keys for k in keys))
    pending_kinds = _count("image" if i["filename"].lower().endswith(IMAGE_EXT) else i["role"] for i in pending)
    estimate = {k: {"pending": n, "sampled": len(sizes.get(k, [])),
                    "mean_bytes_sampled": (sum(sizes[k]) // len(sizes[k])) if sizes.get(k) else None,
                    "extrapolated_bytes": (sum(sizes[k]) // len(sizes[k]) * n) if sizes.get(k) else None}
                for k, n in pending_kinds.items()}
    in_horizon = [f for f in index["filings"] if f["in_horizon"]]
    if pending or failed:
        gaps.append(f"files_incomplete:fetched={len(fetched)} failed={len(failed)} pending={len(pending)}")
    if problems:
        gaps.append(f"payload_problems:{len(problems)}")
    return {"symbol": sym, "generated_at": arch.now_utc(), "benchmark_ready": False, "pit_ready": False,
            "mirror": index["mirror"], "planned_at": index["planned_at"], "horizon": index["horizon"],
            "landing": index["landing"], "forms": forms, "forms_not_offered": index["forms_not_offered"],
            "filings": {"indexed": len(index["filings"]), "in_horizon": len(in_horizon),
                        "beyond_horizon_not_queued": len(index["filings"]) - len(in_horizon),
                        "in_horizon_by_form": _count(f["form"] for f in in_horizon),
                        "in_horizon_all_files_fetched": filings_complete,
                        "earliest": min((f["filing_date"] for f in in_horizon), default=None),
                        "latest": max((f["filing_date"] for f in in_horizon), default=None),
                        "inventory_only_links": _count(x["role"] for f in in_horizon for x in f["inventory_only"])},
            "files": {"queued": len(queue), "fetched": len(fetched), "failed": failed, "pending": len(pending),
                      "pending_images_skipped_by_default": images_pending,
                      "pending_blocked_not_attempted": blocked_pending, "pending_by_kind": pending_kinds,
                      "fetched_bytes": sum(f["bytes"] for f in fetched), "payload_problems": problems,
                      "disk_estimate_pending": estimate, "fetched_items": fetched},
            "findings_by_check": _count(f["check"] for f in index["findings"]), "gaps": gaps,
            "fallback_note": "accepted_at is null; a conservative date-only fallback may be compiled later, "
                             "but no task is marked ready from this mirror"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "fetch", "coverage"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", default="INTC", choices=sorted(MIRRORS))
    parser.add_argument("--forms", nargs="*", default=list(arch.PLAN_FORMS))
    parser.add_argument("--max-pages", type=int, help="per-form page cap for testing (reported as truncated)")
    parser.add_argument("--horizon", help="last filing date to download; default derives from --suite")
    parser.add_argument("--suite", type=Path, default=Path("benchmarks/suites/nasdaq-diagnostic-v1.toml"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--accessions", nargs="*", help="fetch only these accessions, in this order")
    parser.add_argument("--roles", nargs="*", choices=sorted(ROLE_ORDER), help="fetch only these file roles")
    parser.add_argument("--include-images", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="refetch index pages instead of using the cache")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args(argv)
    store = arch.Store(args.root)
    fetcher = make_fetcher(store, timeout=args.timeout, retries=args.retries)
    mirror = MIRRORS[args.symbol]
    if args.command == "plan":
        from scripts.archive_bounds import suite_bounds
        bounds = suite_bounds(args.suite, args.symbol) if args.horizon is None else None
        horizon = args.horizon or bounds["through"]
        date.fromisoformat(horizon)
        result = cmd_plan(fetcher, mirror, tuple(args.forms), args.max_pages, horizon, args.refresh)
        if bounds:
            (store.root / "mirror" / args.symbol / "suite-bounds.json").write_text(json.dumps(bounds, indent=2) + "\n")
    elif args.command == "fetch":
        result = cmd_fetch(fetcher, args.symbol, args.limit, args.accessions, args.include_images, args.retry_failed,
                           args.roles)
    else:
        result = cmd_coverage(store, mirror)
        (store.root / "mirror" / args.symbol / "coverage.json").write_text(json.dumps(result, indent=1) + "\n")
        result = {k: v for k, v in result.items() if k != "files"} | {
            "files": {k: v for k, v in result["files"].items() if k != "fetched_items"}}
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
