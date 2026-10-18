"""Plan, sample-fetch and parse issuer IR (Q4) SEC filing material.

Commands (all offline except ``fetch``):
  plan   read saved all-years feeds from --source store, write plan + reference cross-check
  fetch  fetch a small sample (detail pages, XBRL ZIP, XBRL_HTML) into --root store;
         serial >=1s, 45s timeout, 64MiB per file, request/byte/time budgets, 403/429 stop
  parse  parse fetched ZIP/HTML with the existing XBRL extractors (no available_at)

Feed requests are never issued here: the saved feed is reused. Output is never ready.
"""
import argparse
import gzip
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_rsi.adapters import inline_xbrl as ix  # noqa: E402
from quant_rsi.adapters import q4_filings as q4  # noqa: E402
from quant_rsi.adapters import xbrl_instance as xi  # noqa: E402
from scripts.archive_sources import DEFAULT_USER_AGENT, Store, now_utc, sha256_hex  # noqa: E402

FEED_KEYS = {"META": "meta-financials-all-sec-feed", "AMZN": "amzn-sec-filings-all-sec-feed"}
THROUGH = "2024-10-04"
MAX_FILE = 64 << 20
TIMEOUT = 45.0
INTERVAL = 1.0
STOP_STATUSES = (403, 429)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is recorded as a failure, never followed off the allow-list


class BudgetExceeded(Exception):
    pass


class SampleFetcher:
    """Serial, budgeted, no retries, no redirects. Successful bytes go to the CAS store."""

    def __init__(self, store, *, max_requests=12, max_bytes=64 << 20, max_seconds=360.0,
                 opener=None, sleep=time.sleep, clock=time.monotonic):
        if not 1 <= max_requests <= 250 or not 0 < max_bytes <= 64 << 20 or not 0 < max_seconds <= 360:
            raise ValueError("fetch budget out of range")
        self.store, self.sleep, self.clock = store, sleep, clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect).open
        self.max_requests, self.max_bytes, self.max_seconds = max_requests, max_bytes, max_seconds
        self.requests, self.bytes, self.started, self.last = 0, 0, clock(), None
        self.stopped = None

    def get(self, key, url, meta):
        symbol, fid, kind = meta.get("symbol"), meta.get("filing_id"), meta.get("document_type")
        q4.issuer(symbol)
        if kind == "detail":
            origin = q4.issuer(symbol)["origin"]
            relative = url[len(origin):] if isinstance(url, str) and url.startswith(origin + "/") else ""
            checked, reason = q4.detail_url(relative, symbol, fid)
            valid = checked == url and checked is not None
        else:
            extensions = q4.DOCUMENT_ROLES.get(kind, (None, None, ()))[2]
            valid, reason = q4.check_cdn_url(url, symbol, extensions)
        if not valid:
            raise q4.FeedError(f"fetch_url_refused:{reason}")
        cached = self.store.catalog().get(key)
        if cached and (cached.get("url") != url or cached.get("meta") != meta):
            raise q4.FeedError("cache_identity_mismatch")
        if cached and self.store.read(cached["sha256"]) is not None:
            return dict(cached, cache_hit=True)
        if self.stopped:
            raise BudgetExceeded(self.stopped)
        if self.requests >= self.max_requests:
            raise BudgetExceeded("request_budget")
        if self.bytes >= self.max_bytes:
            raise BudgetExceeded("byte_budget")
        if self.clock() - self.started >= self.max_seconds:
            raise BudgetExceeded("time_budget")
        if self.last is not None and self.clock() - self.last < INTERVAL:
            self.sleep(INTERVAL - (self.clock() - self.last))
        if self.clock() - self.started >= self.max_seconds:
            raise BudgetExceeded("time_budget")
        self.requests += 1
        self.last = self.clock()
        status, body, headers, error, truncated = None, b"", {}, None, False
        cap = min(MAX_FILE, self.max_bytes - self.bytes)
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT,
                                                   "Accept-Encoding": "identity"})
        try:
            with self.opener(req, timeout=TIMEOUT) as resp:
                status, headers = resp.status, dict(resp.headers)
                # One sentinel byte detects over-limit bodies; never save them as complete.
                body = resp.read(cap + 1)
        except urllib.error.HTTPError as err:
            status, headers = err.code, dict(err.headers or {})
            body = err.read(min(1 << 20, cap + 1)) if err.fp else b""
        except http.client.IncompleteRead as err:
            body = err.partial[:cap + 1]
            error = "IncompleteRead: response body incomplete"
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            error = f"{type(err).__name__}: {err}"
        self.last = self.clock()
        if len(body) > cap:
            truncated = True
        declared = headers.get("Content-Length") or headers.get("content-length")
        if status == 200 and declared is not None and (not str(declared).isdigit() or int(declared) != len(body)):
            error = "content_length_mismatch"
        self.bytes += len(body)
        ok = status == 200 and not truncated and error is None
        rec = {"key": key, "url": url, "ok": ok, "status": status, "error": error,
               "error_detail": ("over_64MiB_cap_not_complete" if cap == MAX_FILE else "over_remaining_byte_budget") if truncated else None,
               "bytes": len(body), "at": now_utc(), "meta": meta,
               "content_type": headers.get("Content-Type") or headers.get("content-type"),
               "location": headers.get("Location") or headers.get("location")}
        if body and not truncated:
            rec["sha256"] = self.store.put(body)
        elif body:
            rec["partial_sha256"] = sha256_hex(body)  # hash only; partial bytes are not stored as a blob
        self.store.append(self.store.log_path, rec)
        if status in STOP_STATUSES:
            self.stopped = f"http_{status}"
        elif self.bytes >= self.max_bytes:
            self.stopped = "byte_budget"
        if ok:
            rec["retrieved_at"] = rec["at"]
            self.store.append(self.store.catalog_path, rec)
        return dict(rec, cache_hit=False)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cmd_plan(source, reference_dir, out, through=THROUGH):
    catalog = source.catalog()
    summary = {"through": through, "ready": False, "issuers": {}}
    for symbol, key in FEED_KEYS.items():
        rec = catalog[key]
        data = source.read(rec["sha256"])
        if data is None:
            raise SystemExit(f"missing or corrupt blob for {key}")
        plan = q4.plan_feed(json.loads(data), symbol, source_sha256=rec["sha256"], through=through)
        plan["source"] = {"catalog_key": key, "url": rec["url"], "retrieved_at": rec.get("retrieved_at"),
                          "bytes": rec["bytes"]}
        refs = q4.reference_rows(_read_jsonl(Path(reference_dir) / f"{symbol}.jsonl"), plan["cik"],
                                 through=through)
        check = q4.cross_check(plan, refs)
        _write(out / f"plan-{symbol}.json.gz", plan)
        _write(out / f"crosscheck-{symbol}.json", check)
        summary["issuers"][symbol] = {"plan_stats": plan["stats"], "crosscheck_stats": check["stats"],
                                      "reference_rows": len(refs), "source": plan["source"],
                                      "excluded_identity": plan["excluded"]}
    _write(out / "plan-summary.json", summary)
    return summary


def select_samples(out, extra_10q=4):
    """Fixed 10-K samples plus the latest 10-Q rows (<= through) with a fetchable XBRL ZIP."""
    picks = {"META": [16357264], "AMZN": [16361618]}
    plans = {s: _load(out / f"plan-{s}.json.gz") for s in FEED_KEYS}
    per_issuer = extra_10q // len(plans)
    for symbol, plan in plans.items():
        q = [r for r in plan["rows"] if r["form"] == "10-Q"
             and any(d["document_type"] == "XBRL" and d["fetchable"] for d in r["documents"])]
        q.sort(key=lambda r: r["filing_date"], reverse=True)
        picks[symbol] += [r["raw"]["FilingId"] for r in q[:per_issuer]]
    jobs = []
    for symbol, ids in picks.items():
        rows = {r["raw"]["FilingId"]: r for r in plans[symbol]["rows"]}
        for n, fid in enumerate(ids):
            row = rows[fid]
            kinds = ("XBRL", "XBRL_HTML") if row["form"] == "10-K" else ("XBRL",)
            if n == 0:
                jobs.append((f"q4-detail-{symbol}-{fid}", row["detail_url"], symbol, fid, "detail"))
            for d in row["documents"]:
                if d["document_type"] in kinds and d["fetchable"]:
                    jobs.append((f"q4-{d['document_type'].lower()}-{symbol}-{fid}", d["url"], symbol, fid,
                                 d["document_type"]))
    return jobs


def select_all_xbrl(out):
    """Every planned ZIP, with issuer/date order; no accession synthesized from an IR ID."""
    jobs = []
    for symbol in FEED_KEYS:
        plan = _load(out / f"plan-{symbol}.json.gz")
        for row in sorted(plan["rows"], key=lambda r: (r["filing_date"], r["row_index"])):
            fid = row["raw"]["FilingId"]
            for doc in row["documents"]:
                if doc["document_type"] == "XBRL" and doc["fetchable"]:
                    jobs.append((f"q4-xbrl-{symbol}-{fid}", doc["url"], symbol, fid, "XBRL"))
    return jobs


def cmd_fetch(store, out, max_requests=12, selection="samples"):
    if selection not in ("samples", "all-xbrl"):
        raise ValueError("unknown selection")
    fetcher = SampleFetcher(store, max_requests=max_requests)
    results = []
    jobs = select_all_xbrl(out) if selection == "all-xbrl" else select_samples(out)
    for key, url, symbol, fid, kind in jobs:
        try:
            rec = fetcher.get(key, url, {"symbol": symbol, "filing_id": fid, "document_type": kind})
        except q4.FeedError as err:
            rec = {"key": key, "url": url, "ok": False, "error": str(err)}
        except BudgetExceeded as err:
            rec = {"key": key, "url": url, "ok": False, "error": f"not_attempted:{err}"}
        results.append({k: rec.get(k) for k in ("key", "url", "ok", "status", "bytes", "sha256", "error",
                                                 "error_detail", "cache_hit", "content_type")})
    report = {"selection": selection, "planned": len(jobs),
              "requests_this_run": fetcher.requests, "bytes_this_run": fetcher.bytes,
              "stopped": fetcher.stopped, "results": results, "ready": False}
    _write(out / f"fetch-{now_utc().replace(':', '')}.json", report)
    return report


def _member_inventory(path):
    issues = []
    _, members = ix.read_zip(path, ix.Limits(), issues)
    inline, instance = xi.classify_members(members)
    return {"files": len(members), "names": sorted(members), "inline": inline, "instance": instance,
            "read_issues": issues}


def cmd_parse(store, out, reference_dir):
    catalog = store.catalog()
    symbols = sorted({rec["meta"]["symbol"] for rec in catalog.values()})
    for symbol in symbols:
        q4.issuer(symbol)
    checks = {s: {m["filing_id"]: m for m in json.loads((out / f"crosscheck-{s}.json").read_text())["matches"]
                  if m["filing_id"]} for s in symbols}
    summary = []
    for key, rec in sorted(catalog.items()):
        meta = rec.get("meta") or {}
        symbol, fid, kind = meta.get("symbol"), meta.get("filing_id"), meta.get("document_type")
        data = store.read(rec["sha256"])
        match = checks[symbol].get(fid, {})
        ident = {"filing_id": fid, "symbol": symbol, "accession_candidate": match.get("accession_candidate"),
                 "match_basis": match.get("match_basis"), "crosscheck_status": match.get("status"),
                 "accession_verified": False}
        entry = {"key": key, "document_type": kind, "sha256": rec["sha256"], "bytes": rec["bytes"],
                 "url": rec["url"], "identity": ident, "ready": False}
        if kind == "detail":
            refs = q4.reference_rows(_read_jsonl(Path(reference_dir) / f"{symbol}.jsonl"), q4.issuer(symbol)["cik"])
            plan = _load(out / f"plan-{symbol}.json.gz")
            row = next(r for r in plan["rows"] if r["raw"]["FilingId"] == fid)
            entry["detail_binding"] = q4.detail_sec_links(data, symbol, row, refs)
            text = data.decode("utf-8", "replace")
            entry["detail_mentions"] = {"sec.gov": text.count("sec.gov"), "cloudfront": text.count(q4.CDN_HOST),
                                        "accession_like": sorted(set(re.findall(r"\b[0-9]{10}-[0-9]{2}-[0-9]{6}\b", text)))[:10]}
        elif kind == "XBRL":
            path = _blob_path(store, rec)
            entry["zip"] = _member_inventory(path)
            acc = ident["accession_candidate"]
            if not acc:
                entry["parse"] = "skipped_no_unique_accession_candidate"
            else:
                try:
                    result = xi.extract_any_zip(path, accession=acc, source_url=rec["url"])
                    result["identity"] = ident
                    _write(out / "parsed" / f"{key}.json.gz", result)
                    entry["corroboration"] = corroborate(result, symbol, acc, reference_dir)
                    entry["parse"] = {"document_type": result["document_type"], "counts": result["counts"],
                                      "issue_codes": dict(Counter(i["code"] for i in result["issues"])),
                                      "documents": [d.get("member") for d in result["documents"]]}
                except (ix.ExtractionError, ValueError) as err:
                    entry["parse"] = {"error": f"{type(err).__name__}: {err}"}
        elif kind == "XBRL_HTML":
            entry["html"] = {"has_ix_namespace": ix.IX.encode() in data,
                             "has_ix_header": b"ix:header" in data,
                             "head": data[:300].decode("utf-8", "replace")}
            if ix.IX.encode() in data and ident["accession_candidate"]:
                try:
                    doc = ix.extract_document(data, member=key, provenance={
                        "accession": ident["accession_candidate"], "zip_sha256": None, "source_url": rec["url"],
                        "html_sha256": rec["sha256"]})
                    _write(out / "parsed" / f"{key}.json.gz", {"identity": ident, "document": doc, "ready": False})
                    entry["parse"] = {"facts": len(doc["facts"]),
                                      "by_status": dict(Counter(f["status"] for f in doc["facts"]))}
                except (ix.ExtractionError, ValueError) as err:
                    entry["parse"] = {"error": f"{type(err).__name__}: {err}"}
        summary.append(entry)
    _write(out / "parse-summary.json", {"entries": summary, "ready": False})
    return summary


def corroborate(result, symbol, accession, reference_dir):
    """Content checks against the candidate's reference row. Supports, never verifies, the accession."""
    ref = [r for r in _read_jsonl(Path(reference_dir) / f"{symbol}.jsonl") if r["accessionNumber"] == accession]
    ref = ref[0] if len(ref) == 1 else {}
    dei = {}
    for d in result["documents"]:
        for f in d["facts"]:
            concept = f["concept"]
            namespace = str(concept.get("namespace", ""))
            if not (re.fullmatch(r"http://xbrl\.sec\.gov/dei/[0-9]{4}(?:q[1-4]|-[0-9]{2}-[0-9]{2})?", namespace)
                    or namespace in ("http://xbrl.us/dei/2008-03-31", "http://xbrl.us/dei/2009-01-31")):
                continue
            name = concept.get("local_name")
            if name in ("EntityCentralIndexKey", "DocumentType", "DocumentPeriodEndDate", "AmendmentFlag"):
                if f["status"] == "ok":
                    dei.setdefault(name, set()).add(str(f["value"]))
    members = [d["member"] for d in result["documents"]]
    # Only SEC-extracted inline instance names encode a primary HTML filename.
    # Traditional amzn-YYYYMMDD.xml names do not establish the primary filename.
    stems = [m[:-len("_htm.xml")] + ".htm" for m in members if m.endswith("_htm.xml")]
    def agrees(name, expected):
        values = dei.get(name)
        return values == {expected} if values else None
    checks = {
        "cik": agrees("EntityCentralIndexKey", q4.cik10(q4.issuer(symbol)["cik"])),
        "form": agrees("DocumentType", ref.get("form", "")),
        "period": agrees("DocumentPeriodEndDate", ref.get("reportDate")),
        "member_stem_is_primary_document": stems == [ref.get("primaryDocument")] if len(stems) == len(members) else None,
    }
    disagreement = any(value is False for value in checks.values())
    missing_required = [key for key in ("cik", "form", "period") if checks[key] is None]
    amended = str(ref.get("form", "")).endswith("/A")
    base_with_amendment = (agrees("DocumentType", ref.get("form", "")[:-2]) is True
                           and dei.get("AmendmentFlag") == {"true"}) if amended else None
    return {"dei": {k: sorted(v) for k, v in dei.items()}, "checks": checks,
            "checks_unavailable": [key for key, value in checks.items() if value is None],
            "requires_context_review": disagreement or bool(missing_required),
            "required_content_checks_unavailable": missing_required,
            "base_form_with_amendment_flag_agrees": base_with_amendment,
            "period_comparison_scope": "DEI financial period versus index reportDate; an 8-K event date can differ" if str(ref.get("form", "")).startswith("8-K") else "DEI period versus index reportDate",
            "primary_document_check_basis": "SEC _htm.xml naming convention" if stems else "not_applicable_to_traditional_instance_name",
            "accession_verified": False,
            "note": "Content checks retain disagreements and unavailable evidence; they do not verify accession or publication time."}


def _blob_path(store, rec):
    if store.read(rec["sha256"]) is None:
        raise SystemExit(f"corrupt blob {rec['key']}")
    return store.blob_path(rec["sha256"])


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, sort_keys=True, indent=None if path.suffix == ".gz" else 1, default=str).encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw)


def _load(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("plan", "fetch", "parse"))
    ap.add_argument("--source", default="artifacts/benchmark-quality/issuer-recheck-20261017")
    ap.add_argument("--reference", default="artifacts/benchmark-quality/public-index/import-2026-09-30-v2")
    ap.add_argument("--root", default="artifacts/benchmark-quality/claude/batch-11/q4-store")
    ap.add_argument("--out", default="artifacts/benchmark-quality/claude/batch-11/q4")
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--selection", choices=("samples", "all-xbrl"), default="samples")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if args.command == "plan":
        res = cmd_plan(Store(args.source), args.reference, out)
        print(json.dumps({s: v["crosscheck_stats"] for s, v in res["issuers"].items()}, indent=1))
    elif args.command == "fetch":
        print(json.dumps(cmd_fetch(Store(args.root), out, args.max_requests, args.selection), indent=1))
    else:
        print(json.dumps(cmd_parse(Store(args.root), out, args.reference), indent=1, default=str)[:20000])


if __name__ == "__main__":
    main()
