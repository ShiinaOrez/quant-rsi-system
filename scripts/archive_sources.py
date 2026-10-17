"""Resumable public-archive collector for the nasdaq-diagnostic-v1 benchmark.

Collects raw bytes (content-addressed by sha256, never overwritten) from SEC
EDGAR and public market-data endpoints, and reports coverage gaps.  Nothing
here declares a task ready: readiness is computed from evidence and every
missing, blocked or unverified item is written to the coverage report.

Commands (all take --root):
  targets   list the 20 fixed tasks, 8 symbols and cutoffs from the suite
  probe     check that each upstream endpoint is reachable with this client
  sec-index fetch submissions JSON + every historical shard, merge, check
  plan      build the filing work queue (full .txt as-filed submissions)
  fetch     process pending queue items, --limit N per run, resumable
  market    fetch Yahoo chart history (with splits/dividends) and Nasdaq
            dividend dates; reconstruct session-time raw prices
  coverage  write coverage.json with per-symbol/per-task gaps
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NY = ZoneInfo("America/New_York")
PROJECT_URL = "https://github.com/ShiinaOrez/quant-rsi-system"
DEFAULT_USER_AGENT = f"quant-rsi-system-archive/0.1 (+{PROJECT_URL})"
PLAN_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A")
# Historical spellings that are not in the plan's form list; counted, not dropped silently.
FORM_VARIANTS = ("10-K405", "10-K405/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A", "8-K12B", "8-K12G3")
# Cross-check only: resolution must come from SEC company_tickers.json.
EXPECTED_CIK = {"AAPL": 320193, "INTC": 50863, "MSFT": 789019, "AMZN": 1018724,
                "META": 1326801, "MU": 723125, "PYPL": 1633917, "NVDA": 1045810}
HOST_INTERVAL = {"www.sec.gov": 0.25, "data.sec.gov": 0.25, "efts.sec.gov": 0.25}
DEFAULT_INTERVAL = 1.0
RETRY_STATUSES = (429, 500, 502, 503, 504)


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- targets

def load_targets(suite_path):
    suite_path = Path(suite_path)
    suite = tomllib.loads(suite_path.read_text())
    base = suite_path.parent
    entries = [{"task": t, "assessor": None, "data_plan": None} for t in suite.get("legacy_tasks", [])]
    entries += [dict(c) for c in suite["cases"]]
    tasks = []
    for entry in entries:
        task = tomllib.loads((base / entry["task"]).read_text())
        plan_rel = entry.get("data_plan")
        plan = None
        if plan_rel is None:
            # Legacy tasks may still have a same-named data plan next to the others.
            guess = base / "../data-plans" / (task["id"] + ".toml")
            plan_rel = "../data-plans/" + guess.name if guess.exists() else None
        if plan_rel:
            plan = tomllib.loads((base / plan_rel).read_text())
        tasks.append({"task_id": task["id"], "symbol": task["instrument"]["symbol"],
                      "as_of": task["episode"]["as_of"],
                      "horizon_sessions": task["episode"]["horizon_sessions"],
                      "first_session": entry.get("first_session"),
                      "terminal_session": entry.get("terminal_session"),
                      "data_plan": plan_rel, "data_plan_status": plan.get("status") if plan else None,
                      "plan_cik": (plan or {}).get("identity", {}).get("cik")})
    expected = suite.get("expected_task_count")
    if expected is not None and len(tasks) != expected:
        raise ValueError(f"suite lists {len(tasks)} tasks, expected {expected}")
    if len({t["task_id"] for t in tasks}) != len(tasks):
        raise ValueError("duplicate task ids in suite")
    return tasks


# ---------------------------------------------------------------- storage

class Store:
    """Content-addressed blob store plus append-only fetch log and catalog."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "logs" / "fetch-log.jsonl"
        self.catalog_path = self.root / "catalog.jsonl"

    def blob_path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid content digest")
        return self.root / "blobs" / digest[:2] / digest

    def put(self, data):
        digest = sha256_hex(data)
        path = self.blob_path(digest)
        if path.exists():
            if sha256_hex(path.read_bytes()) == digest:
                return digest
            self._quarantine(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return digest

    def read(self, digest):
        """Return verified bytes or None if missing/corrupt (corrupt files are quarantined)."""
        path = self.blob_path(digest)
        if not path.exists():
            return None
        data = path.read_bytes()
        if sha256_hex(data) != digest:
            self._quarantine(path)
            return None
        return data

    def _quarantine(self, path):
        target = self.root / "quarantine" / f"{path.name}.{int(time.time() * 1000)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
        self.append(self.log_path, {"event": "quarantine", "path": str(path), "moved_to": str(target),
                                    "at": now_utc()})

    @staticmethod
    def append(path, record):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def catalog(self):
        """Latest successful version per key; older versions stay in catalog.jsonl."""
        latest = {}
        if self.catalog_path.exists():
            for line in self.catalog_path.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    latest[rec["key"]] = rec
        return latest

    def versions(self, key):
        out = []
        if self.catalog_path.exists():
            for line in self.catalog_path.read_text().splitlines():
                if line.strip() and json.loads(line)["key"] == key:
                    out.append(json.loads(line))
        return out

    def last_failures(self):
        failures = {}
        if self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                rec = json.loads(line) if line.strip() else {}
                if "key" not in rec:
                    continue
                if rec.get("ok"):
                    failures.pop(rec["key"], None)
                else:
                    failures[rec["key"]] = rec
        return failures


# ---------------------------------------------------------------- http

class HostBlocked(Exception):
    pass


class Fetcher:
    """Serial HTTP client with per-host minimum interval, retries and a 403 breaker."""

    def __init__(self, store, user_agent=DEFAULT_USER_AGENT, timeout=30.0, retries=4,
                 max_consecutive_403=3, opener=None, sleep=time.sleep, clock=time.monotonic,
                 intervals=None):
        self.store, self.user_agent, self.timeout, self.retries = store, user_agent, timeout, retries
        self.max_consecutive_403 = max_consecutive_403
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.clock = sleep, clock
        self.intervals = dict(HOST_INTERVAL, **(intervals or {}))
        self.last_request = {}
        self.consecutive_403 = {}
        self.blocked = set()

    def _host(self, url):
        return urllib.request.urlparse(url).hostname

    def _wait(self, host):
        interval = self.intervals.get(host, DEFAULT_INTERVAL)
        last = self.last_request.get(host)
        if last is not None:
            delay = interval - (self.clock() - last)
            if delay > 0:
                self.sleep(delay)
        self.last_request[host] = self.clock()

    def _request(self, url, headers):
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                   "Accept-Encoding": "identity", **headers})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers or {}), err.read() if err.fp else b""

    def get(self, key, url, refresh=False, headers=None, meta=None):
        """Return a catalog record; cached verified bytes are reused unless refresh."""
        cached = self.store.catalog().get(key)
        if cached and not refresh and self.store.read(cached["sha256"]) is not None:
            return dict(cached, cache_hit=True)
        host = self._host(url)
        if host in self.blocked:
            rec = {"key": key, "url": url, "ok": False, "status": None, "error": "host_blocked_this_run",
                   "at": now_utc()}
            self.store.append(self.store.log_path, rec)
            raise HostBlocked(host)
        attempt, status, body, resp_headers, error = 0, None, b"", {}, None
        while True:
            attempt += 1
            self._wait(host)
            try:
                status, resp_headers, body = self._request(url, headers or {})
                error = None
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                status, body, resp_headers, error = None, b"", {}, f"{type(err).__name__}: {err}"
            retryable = error is not None or status in RETRY_STATUSES
            if not retryable or attempt > self.retries:
                break
            retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
            backoff = min(60.0, 2.0 * 2 ** (attempt - 1))
            if retry_after and str(retry_after).isdigit():
                backoff = min(120.0, max(backoff, float(retry_after)))
            self.sleep(backoff)
        ok = status == 200
        rec = {"key": key, "url": url, "ok": ok, "status": status, "attempts": attempt,
               "error": error, "at": now_utc(), "bytes": len(body),
               "retry_after": resp_headers.get("Retry-After") or resp_headers.get("retry-after"),
               "content_type": resp_headers.get("Content-Type") or resp_headers.get("content-type")}
        if body:
            # Error bodies are kept too: a 403 page is evidence of the block.
            rec["sha256"] = self.store.put(body)
        if meta:
            rec["meta"] = meta
        self.store.append(self.store.log_path, rec)
        if status == 403:
            self.consecutive_403[host] = self.consecutive_403.get(host, 0) + 1
            if self.consecutive_403[host] >= self.max_consecutive_403:
                self.blocked.add(host)
        elif status is not None:
            self.consecutive_403[host] = 0
        if ok:
            rec["retrieved_at"] = rec["at"]
            self.store.append(self.store.catalog_path, rec)
        return dict(rec, cache_hit=False)


# ---------------------------------------------------------------- SEC

def cik10(cik):
    return f"{int(cik):010d}"


def resolve_ciks(ticker_json, symbols):
    """Map symbols to CIK from SEC company_tickers.json; disagreement with the pinned list is an error."""
    rows = ticker_json.values() if isinstance(ticker_json, dict) else ticker_json
    by_ticker = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"].upper(), set()).add(int(row["cik_str"]))
    out, problems = {}, []
    for sym in symbols:
        found = by_ticker.get(sym.upper(), set())
        if len(found) != 1:
            problems.append({"symbol": sym, "problem": "unresolved_or_ambiguous", "candidates": sorted(found)})
            continue
        cik = found.pop()
        if sym in EXPECTED_CIK and EXPECTED_CIK[sym] != cik:
            problems.append({"symbol": sym, "problem": "cik_mismatch_vs_pinned", "sec": cik,
                             "pinned": EXPECTED_CIK[sym]})
        out[sym] = cik
    return out, problems


def columnar_rows(block):
    keys = [k for k, v in block.items() if isinstance(v, list)]
    lengths = {len(block[k]) for k in keys}
    if len(lengths) > 1:
        raise ValueError(f"ragged submissions columns: { {k: len(block[k]) for k in keys} }")
    n = lengths.pop() if lengths else 0
    return [{k: block[k][i] for k in keys} for i in range(n)]


def merge_submissions(main, shards):
    """Merge recent + historical shards; dedupe by accession; return rows and check findings."""
    findings = []
    listed = {f["name"]: f for f in main.get("filings", {}).get("files", [])}
    for name in listed:
        if name not in shards:
            findings.append({"check": "shard_missing", "shard": name, "severity": "gap"})
    sources = [("recent", columnar_rows(main["filings"]["recent"]))]
    for name, shard in sorted(shards.items()):
        rows = columnar_rows(shard)
        meta = listed.get(name, {})
        if meta.get("filingCount") is not None and meta["filingCount"] != len(rows):
            findings.append({"check": "shard_count_mismatch", "shard": name, "listed": meta["filingCount"],
                             "actual": len(rows), "severity": "error"})
        for row in rows:
            if meta.get("filingFrom") and not meta["filingFrom"] <= row["filingDate"] <= meta["filingTo"]:
                findings.append({"check": "shard_date_out_of_range", "shard": name,
                                 "accession": row["accessionNumber"], "severity": "error"})
                break
        sources.append((name, rows))
    merged = {}
    for source, rows in sources:
        for row in rows:
            acc = row["accessionNumber"]
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
                findings.append({"check": "bad_accession", "accession": acc, "severity": "error"})
                continue
            row = dict(row, _source=source)
            if acc in merged:
                prev = merged[acc]
                for field in ("form", "filingDate", "acceptanceDateTime"):
                    if prev.get(field) != row.get(field):
                        findings.append({"check": "duplicate_conflict", "accession": acc, "field": field,
                                         "values": [prev.get(field), row.get(field)], "severity": "error"})
                continue
            merged[acc] = row
    return sorted(merged.values(), key=lambda r: (r["filingDate"], r["accessionNumber"])), findings


def filing_txt_url(cik, accession):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def parse_submission_header(data):
    """Parse the SGML header and document inventory of a full submission .txt."""
    head = data[:200000].decode("latin-1")
    out = {}
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", head)
    if m:
        out["acceptance_raw"] = m.group(1)
        out["accepted_at"] = datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=NY).isoformat()
    for label, key in (("ACCESSION NUMBER", "accession"), ("CONFORMED SUBMISSION TYPE", "form"),
                       ("PUBLIC DOCUMENT COUNT", "public_document_count"),
                       ("CONFORMED PERIOD OF REPORT", "period_of_report"), ("FILED AS OF DATE", "filed_as_of")):
        m = re.search(rf"^{label}:\s*(\S+)", head, re.M)
        if m:
            out[key] = m.group(1)
    docs = []
    for m in re.finditer(rb"<DOCUMENT>\s*<TYPE>([^\r\n]+)\s*<SEQUENCE>([^\r\n]*)\s*<FILENAME>([^\r\n]*)"
                         rb"(?:\s*<DESCRIPTION>([^\r\n]*))?", data):
        docs.append({"type": m.group(1).decode("latin-1").strip(),
                     "sequence": m.group(2).decode("latin-1").strip(),
                     "filename": m.group(3).decode("latin-1").strip(),
                     "description": (m.group(4) or b"").decode("latin-1").strip()})
    out["documents"] = docs
    out["document_open_count"] = len(re.findall(rb"<DOCUMENT>", data))
    out["document_close_count"] = len(re.findall(rb"</DOCUMENT>", data))
    out["submission_terminated"] = data.rstrip().endswith(b"</SEC-DOCUMENT>")
    return out


def check_filing(row, header):
    """Consistency between submissions JSON and the as-filed header."""
    problems = []
    if header.get("accession") != row["accessionNumber"]:
        problems.append("accession_mismatch")
    if header.get("form") != row["form"]:
        problems.append("form_mismatch")
    if "accepted_at" not in header:
        problems.append("acceptance_datetime_missing_in_header")
    else:
        # submissions JSON carries a 'Z' suffix; compare clock digits and record which reading matched.
        digits = re.sub(r"\D", "", (row.get("acceptanceDateTime") or ""))[:14]
        # Old index shards can lack this field. The original submission header
        # is the timestamp evidence; an absent redundant cross-check is allowed.
        if digits and digits != header["acceptance_raw"]:
            as_utc = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            if as_utc.astimezone(NY).strftime("%Y%m%d%H%M%S") != header["acceptance_raw"]:
                problems.append("acceptance_datetime_disagrees")
    if not header.get("documents"):
        problems.append("no_documents_parsed")
    count = header.get("public_document_count", "")
    if not str(count).isdigit():
        problems.append("public_document_count_missing_or_invalid")
    elif int(count) != len(header["documents"]):
        problems.append("public_document_count_mismatch")
    if (header["document_open_count"] != header["document_close_count"]
            or header["document_open_count"] != len(header["documents"])):
        problems.append("document_inventory_incomplete")
    if not header["submission_terminated"]:
        problems.append("submission_truncated")
    return problems


# ---------------------------------------------------------------- market data

def parse_yahoo_chart(data):
    chart = json.loads(data)["chart"]
    if chart.get("error") or not chart.get("result"):
        raise ValueError(f"yahoo chart error: {chart.get('error')}")
    res = chart["result"][0]
    q = res["indicators"]["quote"][0]
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "America/New_York")
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        bars.append({"session": datetime.fromtimestamp(ts, tz).date().isoformat(),
                     **{f: q[f][i] for f in ("open", "high", "low", "close", "volume")}})
    events = res.get("events") or {}
    splits = sorted(({"session": datetime.fromtimestamp(int(s["date"]), tz).date().isoformat(),
                      "ratio": float(s["numerator"]) / float(s["denominator"]), "label": s.get("splitRatio")}
                     for s in events.get("splits", {}).values()), key=lambda s: s["session"])
    dividends = sorted(({"ex_session": datetime.fromtimestamp(int(d["date"]), tz).date().isoformat(),
                         "amount_split_adjusted": d["amount"]} for d in events.get("dividends", {}).values()),
                       key=lambda d: d["ex_session"])
    return {"meta": res["meta"], "bars": bars, "splits": splits, "dividends": dividends}


def reconstruct_raw(chart):
    """Undo retroactive split adjustment: Yahoo OHLC/volume (even auto_adjust=False) are restated
    for every split after the session, so multiply prices / divide volume by later split ratios."""
    splits = chart["splits"]

    def factor(session):
        f = 1.0
        for s in splits:
            if s["session"] > session:
                f *= s["ratio"]
        return f

    rows, issues = [], []
    for bar in chart["bars"]:
        if any(bar[k] is None for k in ("open", "high", "low", "close", "volume")):
            issues.append({"session": bar["session"], "issue": "null_field"})
            continue
        f = factor(bar["session"])
        volume = bar["volume"] / f
        if abs(volume - round(volume)) > 1e-6:
            issues.append({"session": bar["session"], "issue": "non_integer_raw_volume", "value": volume})
        rows.append({"session": bar["session"], "split_factor_after": f,
                     **{f"raw_{k}": round(bar[k] * f, 6) for k in ("open", "high", "low", "close")},
                     "raw_volume": volume})
    dividends = [dict(d, amount_reconstructed_raw=round(d["amount_split_adjusted"] * factor(d["ex_session"]), 6))
                 for d in chart["dividends"]]
    return {"bars": rows, "dividends": dividends, "issues": issues,
            "normalization_verified": False,
            "note": "candidate reconstruction; volume convention and price precision require source verification"}


def parse_nasdaq_dividends(data):
    rows = ((json.loads(data).get("data") or {}).get("dividends") or {}).get("rows") or []

    def iso(v):
        try:
            return datetime.strptime(v, "%m/%d/%Y").date().isoformat()
        except (TypeError, ValueError):
            return None
    return [{"ex_date": iso(r.get("exOrEffDate")), "declaration_date": iso(r.get("declarationDate")),
             "record_date": iso(r.get("recordDate")), "payment_date": iso(r.get("paymentDate")),
             "amount": r.get("amount"), "type": r.get("type")} for r in rows]


def yahoo_url(symbol, period2):
    return (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1=0&period2={period2}"
            f"&interval=1d&events=div%7Csplit&includeAdjustedClose=true")


def nasdaq_dividends_url(symbol):
    return f"https://api.nasdaq.com/api/quote/{symbol}/dividends?assetclass=stocks"


# ---------------------------------------------------------------- commands

def _json_blob(store, rec):
    data = store.read(rec["sha256"]) if rec and rec.get("ok") else None
    return json.loads(data) if data else None


def cmd_probe(fetcher, symbols):
    """Reachability with *this* client (urllib), recorded with status and body sha."""
    probes = [("sec_company_tickers", "https://www.sec.gov/files/company_tickers.json"),
              ("sec_submissions_INTC", f"https://data.sec.gov/submissions/CIK{cik10(50863)}.json"),
              ("sec_archives_INTC_dir", "https://www.sec.gov/Archives/edgar/data/50863/"),
              ("yahoo_chart_NVDA_2024_06", "https://query1.finance.yahoo.com/v8/finance/chart/NVDA?"
               "period1=1717372800&period2=1718409600&interval=1d&events=div%7Csplit"),
              ("nasdaq_dividends_NVDA", nasdaq_dividends_url("NVDA"))]
    results = []
    for name, url in probes:
        try:
            rec = fetcher.get(f"probe:{name}:{now_utc()[:10]}", url, refresh=True,
                              headers={"Accept": "application/json"} if "nasdaq" in url else None)
        except HostBlocked as host:
            results.append({"probe": name, "url": url, "status": None, "error": f"blocked:{host}"})
            continue
        entry = {"probe": name, "url": url, "status": rec["status"], "bytes": rec["bytes"],
                 "sha256": rec.get("sha256"), "error": rec.get("error")}
        body = fetcher.store.read(rec["sha256"]) if rec.get("sha256") else b""
        if rec["status"] == 403 and b"Undeclared Automated Tool" in (body or b""):
            entry["diagnosis"] = "sec_undeclared_automated_tool_block"
        elif rec["status"] == 403 and b"Request Rate Threshold Exceeded" in (body or b""):
            entry["diagnosis"] = "sec_rate_threshold_block"
        if name.startswith("yahoo") and rec["ok"]:
            chart = parse_yahoo_chart(body)
            raw = {b["session"]: b for b in reconstruct_raw(chart)["bars"]}
            adj = {b["session"]: b for b in chart["bars"]}
            if "2024-06-07" in raw:
                entry["anchor_2024_06_07"] = {"provider_close": adj["2024-06-07"]["close"],
                                              "provider_volume": adj["2024-06-07"]["volume"],
                                              "reconstructed_raw_close": raw["2024-06-07"]["raw_close"],
                                              "reconstructed_raw_volume": raw["2024-06-07"]["raw_volume"],
                                              "splits": chart["splits"]}
        if name.startswith("nasdaq") and rec["ok"]:
            rows = parse_nasdaq_dividends(body)
            entry["dividend_rows"] = len(rows)
            entry["has_payment_date"] = sum(1 for r in rows if r["payment_date"])
        results.append(entry)
    return results


def cmd_sec_index(fetcher, symbols, refresh=False):
    store = fetcher.store
    out = {"at": now_utc(), "symbols": {}, "problems": []}
    try:
        rec = fetcher.get("sec:company_tickers", "https://www.sec.gov/files/company_tickers.json", refresh)
    except HostBlocked:
        rec = None
    tickers = _json_blob(store, rec)
    if tickers is None:
        out["problems"].append({"problem": "company_tickers_unavailable",
                                "status": rec and rec.get("status")})
        return out
    ciks, problems = resolve_ciks(tickers, symbols)
    out["problems"] += problems
    mismatches = {p["symbol"] for p in problems if p["problem"] == "cik_mismatch_vs_pinned"}
    for sym, cik in ciks.items():
        entry = {"cik": cik10(cik)}
        out["symbols"][sym] = entry
        if sym in mismatches:
            entry["status"] = "identity_mismatch_blocked"
            continue
        try:
            main_rec = fetcher.get(f"sec:submissions:{cik10(cik)}",
                                   f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json", refresh)
            main = _json_blob(store, main_rec)
            if main is None:
                entry["status"] = f"submissions_http_{main_rec.get('status')}"
                continue
            if str(main.get("cik", "")).lstrip("0") != str(cik).lstrip("0"):
                entry["status"] = "submissions_cik_missing_or_mismatch"
                out["problems"].append({"symbol": sym, "problem": entry["status"]})
                continue
            if sym not in [t.upper() for t in main.get("tickers", [])]:
                out["problems"].append({"symbol": sym, "problem": "ticker_not_in_submissions_tickers",
                                        "tickers": main.get("tickers")})
                entry["status"] = "submissions_ticker_mismatch_blocked"
                continue
            shards = {}
            for f in main["filings"].get("files", []):
                srec = fetcher.get(f"sec:submissions-shard:{f['name']}",
                                   f"https://data.sec.gov/submissions/{f['name']}", refresh)
                shard = _json_blob(store, srec)
                if shard is not None:
                    shards[f["name"]] = shard
            rows, findings = merge_submissions(main, shards)
        except HostBlocked as host:
            entry["status"] = f"blocked:{host}"
            continue
        index_dir = store.root / "sec" / cik10(cik)
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "filings-index.json").write_text(json.dumps(
            {"symbol": sym, "cik": cik10(cik), "built_at": now_utc(), "main_sha256": main_rec["sha256"],
             "shards": sorted(shards), "findings": findings, "rows": rows}, indent=1))
        entry.update(status="indexed", filings_total=len(rows), shards=len(shards), findings=len(findings))
    return out


def cmd_plan(store, forms=PLAN_FORMS, through=None):
    """Work queue: one full as-filed submission .txt per in-scope filing (main doc + all exhibits)."""
    items, summary = [], {}
    for path in sorted((store.root / "sec").glob("*/filings-index.json")):
        index = json.loads(path.read_text())
        variants = {}
        n = 0
        est = 0
        for row in index["rows"]:
            if through is not None and row["filingDate"] > through:
                continue
            if row["form"] in forms:
                n += 1
                est += int(row.get("size") or 0)
                items.append({"key": f"sec:filing-txt:{row['accessionNumber']}", "symbol": index["symbol"],
                              "cik": index["cik"], "accession": row["accessionNumber"], "form": row["form"],
                              "filing_date": row["filingDate"], "report_date": row.get("reportDate"),
                              "submissions_acceptance": row.get("acceptanceDateTime"),
                              "url": filing_txt_url(index["cik"], row["accessionNumber"])})
            elif row["form"] in FORM_VARIANTS:
                variants[row["form"]] = variants.get(row["form"], 0) + 1
        summary[index["symbol"]] = {"in_scope": n, "estimated_bytes": est,
                                    "out_of_plan_form_variants": variants}
    items.sort(key=lambda i: (i["symbol"], i["filing_date"], i["accession"]))
    (store.root / "plan.jsonl").write_text("".join(json.dumps(i, sort_keys=True) + "\n" for i in items))
    return {"items": len(items), "per_symbol": summary}


def load_plan(store):
    path = store.root / "plan.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def cmd_fetch(fetcher, limit, symbols=None, retry_failed=False):
    store = fetcher.store
    catalog, failures = store.catalog(), store.last_failures()
    done = processed = 0
    results = {"fetched": 0, "failed": 0, "skipped_cached": 0, "blocked": 0}
    for item in load_plan(store):
        if symbols and item["symbol"] not in symbols:
            continue
        if item["key"] in catalog and store.read(catalog[item["key"]]["sha256"]) is not None:
            results["skipped_cached"] += 1
            continue
        if item["key"] in failures and not retry_failed and failures[item["key"]].get("status") in (403, 404):
            continue  # recorded in the log and reported by coverage; use --retry-failed to try again
        if processed >= limit:
            break
        processed += 1
        try:
            rec = fetcher.get(item["key"], item["url"], meta={"symbol": item["symbol"], "form": item["form"]})
        except HostBlocked:
            results["blocked"] += 1
            break
        results["fetched" if rec["ok"] else "failed"] += 1
    results["processed"] = processed
    return results


def cmd_market(fetcher, symbols, refresh=False):
    store = fetcher.store
    period2 = int(time.time())
    out = {}
    for sym in symbols:
        entry = {}
        for key, url, hdr in ((f"yahoo:chart:{sym}", yahoo_url(sym, period2), None),
                              (f"nasdaq:dividends:{sym}", nasdaq_dividends_url(sym), {"Accept": "application/json"})):
            try:
                rec = fetcher.get(key, url, refresh, headers=hdr)
                entry[key.split(":")[0]] = {"status": rec["status"], "sha256": rec.get("sha256"),
                                            "error": rec.get("error"), "cache_hit": rec["cache_hit"],
                                            "retrieved_at": rec.get("retrieved_at") or rec.get("at")}
            except HostBlocked as host:
                entry[key.split(":")[0]] = {"status": None, "error": f"blocked:{host}"}
        out[sym] = entry
    return out


def _session_counts(sessions, cutoff_day):
    before = sum(1 for s in sessions if s <= cutoff_day)
    after = sum(1 for s in sessions if s > cutoff_day)
    return before, after


def cmd_coverage(store, tasks):
    catalog, failures = store.catalog(), store.last_failures()
    plan = load_plan(store)
    symbols = sorted({t["symbol"] for t in tasks})
    report = {"generated_at": now_utc(), "benchmark_ready": False, "symbols": {}, "tasks": [],
              "global_blockers": [
                  "ohlcv/corporate-action/calendar providers are UNSELECTED in us-equity-history-v1 and"
                  " no data license has been reviewed",
                  "three-statement extraction from versioned filings is not implemented",
                  "Yahoo history is a current download restated for later splits, not a historical vintage",
                  "official dividend pay dates not yet bound to issuer filings",
                  "amends_accession for /A filings is not resolved",
                  "provider sessions not yet audited against the XNAS calendar (session counts are raw rows)"]}
    indexes = {}
    for path in (store.root / "sec").glob("*/filings-index.json"):
        index = json.loads(path.read_text())
        indexes[index["symbol"]] = index
    for sym in symbols:
        s = {"sec": {}, "market": {}, "gaps": []}
        index = indexes.get(sym)
        if index is None:
            s["gaps"].append("sec_submissions_index_missing")
        else:
            items = [i for i in plan if i["symbol"] == sym]
            fetched = [i for i in items if i["key"] in catalog]
            failed = [{"accession": i["accession"], "status": failures[i["key"]].get("status"),
                       "error": failures[i["key"]].get("error")} for i in items
                      if i["key"] in failures and i["key"] not in catalog]
            accepted, problems, exhibits = 0, [], 0
            for i in fetched:
                data = store.read(catalog[i["key"]]["sha256"])
                if data is None:
                    problems.append({"accession": i["accession"], "problem": "blob_missing_or_corrupt"})
                    continue
                header = parse_submission_header(data)
                row = {"accessionNumber": i["accession"], "form": i["form"],
                       "acceptanceDateTime": i["submissions_acceptance"]}
                issues = check_filing(row, header)
                if issues:
                    problems.append({"accession": i["accession"], "problem": issues})
                if "accepted_at" in header and not issues:
                    accepted += 1
                exhibits += sum(1 for d in header["documents"] if d["type"].startswith("EX-"))
            s["sec"] = {"cik": index["cik"], "filings_total": len(index["rows"]), "in_scope": len(items),
                        "fetched": len(fetched), "failed": failed, "pending": len(items) - len(fetched) - len(failed),
                        "accepted_at_verified": accepted, "exhibit_documents": exhibits,
                        "index_findings": index["findings"], "filing_problems": problems,
                        "complete": bool(items) and len(fetched) == len(items) and accepted == len(items)
                        and not index["findings"]}
            if not s["sec"]["complete"]:
                s["gaps"].append(f"sec_filings_incomplete:{len(fetched)}/{len(items)}"
                                 f" accepted_at_verified:{accepted}")
            if index["findings"]:
                s["gaps"].append(f"sec_index_findings:{len(index['findings'])}")
        chart_rec = catalog.get(f"yahoo:chart:{sym}")
        sessions = []
        if chart_rec:
            chart = parse_yahoo_chart(store.read(chart_rec["sha256"]))
            raw = reconstruct_raw(chart)
            sessions = [b["session"] for b in raw["bars"]]
            s["market"]["yahoo"] = {"retrieved_at": chart_rec["retrieved_at"], "sha256": chart_rec["sha256"],
                                    "first_session": sessions[0] if sessions else None,
                                    "last_session": sessions[-1] if sessions else None,
                                    "sessions": len(sessions), "splits": chart["splits"],
                                    "dividends": len(chart["dividends"]), "issues": raw["issues"][:20],
                                    "vintage": "current_download_split_restated_raw_reconstructed",
                                    "volume_basis": "unverified_consolidated_vs_listing_venue"}
            s["gaps"].append("ohlcv_not_historical_vintage_and_unlicensed")
            fetched_ny = datetime.fromisoformat(chart_rec["retrieved_at"]).astimezone(NY)
            if sessions and sessions[-1] == fetched_ny.date().isoformat() and fetched_ny.hour < 16:
                s["gaps"].append(f"last_bar_{sessions[-1]}_fetched_intraday_exclude")
        else:
            fail = failures.get(f"yahoo:chart:{sym}")
            s["gaps"].append(f"ohlcv_missing:{fail and fail.get('status')}")
        div_rec = catalog.get(f"nasdaq:dividends:{sym}")
        if div_rec:
            rows = parse_nasdaq_dividends(store.read(div_rec["sha256"]))
            s["market"]["nasdaq_dividends"] = {"rows": len(rows), "with_payment_date":
                                               sum(1 for r in rows if r["payment_date"]),
                                               "role": "third_party_cross_check_not_official_pay_date"}
        else:
            fail = failures.get(f"nasdaq:dividends:{sym}") or {}
            s["gaps"].append(f"nasdaq_dividend_dates_missing:{fail.get('status') or fail.get('error')}")
        s["gaps"].append("official_pay_dates_unbound")
        s["_sessions"] = sessions  # used for task coverage below, removed before serialising
        report["symbols"][sym] = s
    for t in tasks:
        sym = report["symbols"][t["symbol"]]
        cutoff_day = t["as_of"][:10]
        before, after = _session_counts(sym["_sessions"], cutoff_day)
        gaps = list(sym["gaps"])
        if t["data_plan"] is None:
            gaps.append("task_has_no_data_plan")
        if before < 252:
            gaps.append(f"warmup_sessions_{before}_lt_252")
        if after < t["horizon_sessions"]:
            gaps.append(f"forward_sessions_{after}_lt_{t['horizon_sessions']}")
        report["tasks"].append({"task_id": t["task_id"], "symbol": t["symbol"], "as_of": t["as_of"],
                                "data_plan": t["data_plan"], "sessions_before_cutoff": before,
                                "sessions_after_cutoff": after, "pit_ready": False, "gaps": gaps})
    for sym in report["symbols"].values():
        sym.pop("_sessions")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["targets", "probe", "sec-index", "plan", "fetch", "market", "coverage"])
    parser.add_argument("--root", type=Path, required=True, help="archive directory (blobs, logs, catalog)")
    parser.add_argument("--suite", type=Path, default=Path("benchmarks/suites/nasdaq-diagnostic-v1.toml"))
    parser.add_argument("--symbols", nargs="*", help="restrict to these symbols")
    parser.add_argument("--limit", type=int, default=50, help="max queue items per fetch run")
    parser.add_argument("--user-agent", default=os.environ.get("ARCHIVE_SEC_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--refresh", action="store_true", help="fetch a new version even if cached")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--through", help="last filing date for plan; default derives from --suite")
    args = parser.parse_args(argv)
    tasks = load_targets(args.suite)
    symbols = args.symbols or sorted({t["symbol"] for t in tasks})
    store = Store(args.root)
    fetcher = Fetcher(store, user_agent=args.user_agent, timeout=args.timeout, retries=args.retries)
    if args.command == "targets":
        result = {"tasks": tasks, "symbols": sorted({t["symbol"] for t in tasks}),
                  "cutoffs": sorted({t["as_of"] for t in tasks})}
    elif args.command == "probe":
        result = {"at": now_utc(), "client": "python-urllib", "user_agent": args.user_agent,
                  "results": cmd_probe(fetcher, symbols)}
    elif args.command == "sec-index":
        result = cmd_sec_index(fetcher, symbols, args.refresh)
    elif args.command == "plan":
        from scripts.archive_bounds import suite_bounds
        through = args.through or suite_bounds(args.suite)["through"]
        datetime.fromisoformat(through)
        result = cmd_plan(store, through=through)
        result["through"] = through
    elif args.command == "fetch":
        result = cmd_fetch(fetcher, args.limit, set(args.symbols or []), args.retry_failed)
    elif args.command == "market":
        result = cmd_market(fetcher, symbols, args.refresh)
    else:
        result = cmd_coverage(store, tasks)
        (store.root / "coverage.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
