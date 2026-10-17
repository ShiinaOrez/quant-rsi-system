"""Bounded Wayback Machine collector for archived SEC EDGAR submissions indexes.

The Internet Archive is a public third-party archive, not SEC.  A verified
capture proves byte identity with the archived copy only.  Every network
request goes to web.archive.org (never to sec.gov / data.sec.gov directly),
serially, with a global minimum interval; the first 403/429 stops the host for
the rest of the run.  Nothing here binds a publication time to a filing:
acceptanceDateTime is kept as the raw string and available_at stays null.

Commands (all take --root, a Store directory):
  import   independently re-verify saved batch-06 captures (raw bytes against
           the saved CDX row) and register them, to avoid slow re-downloads
  collect  CDX-discover + id_-replay the main submissions JSON and every
           historical shard it lists, per symbol
  index    merge recent + shards, filter by the suite's latest terminal
           session, write index/<SYMBOL>.json and gaps.json (offline)
"""
import argparse
import base64
import hashlib
import http.client
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.archive_sources import (EXPECTED_CIK, FORM_VARIANTS, PLAN_FORMS, Store, cik10, columnar_rows,
                                     merge_submissions, now_utc, sha256_hex)

WAYBACK = "web.archive.org"
SEC_HOSTS = ("data.sec.gov", "www.sec.gov")
DEFAULT_SYMBOLS = ("AAPL", "NVDA", "MSFT")
MIN_INTERVAL = 2.0
# Response-body caps are on transport bytes; decoded cap bounds gzip expansion.
LIMITS = {"cdx_bytes": 2_000_000, "body_bytes": 4_000_000, "decoded_bytes": 40_000_000, "seconds": 90.0}
CDX_FIELDS = ("timestamp", "original", "mimetype", "statuscode", "digest", "length")
CDX_LIMIT = 100
SHARD_RE = re.compile(r"CIK(\d{10})-submissions-(\d{3})\.json")
DIGEST_BASIS = "sha1(raw transport bytes) base32 == CDX digest"
USER_AGENT = "quant-rsi-system-archive/0.1 (+https://github.com/ShiinaOrez/quant-rsi-system)"


class HostStopped(Exception):
    pass


def sha1_b32(data):
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


# ---------------------------------------------------------------- URLs

def check_original(url):
    """Only plain https SEC URLs without query, credentials, port or traversal are accepted."""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in SEC_HOSTS or parts.port is not None
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.netloc != parts.hostname):
        raise ValueError(f"original URL rejected: {url!r}")
    if any(seg in ("..", ".") for seg in parts.path.split("/")) or "%" in parts.path or "\\" in parts.path:
        raise ValueError(f"original URL path rejected: {url!r}")
    return url


def submissions_url(cik):
    return f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json"


def shard_url(cik, name):
    """Shard names come only from main files[]; must be CIK{same cik}-submissions-NNN.json."""
    match = SHARD_RE.fullmatch(name) if isinstance(name, str) else None
    if not match or match.group(1) != cik10(cik):
        raise ValueError(f"shard name rejected: {name!r}")
    return check_original(f"https://data.sec.gov/submissions/{name}")


def cdx_url(original, limit=CDX_LIMIT):
    query = {"url": check_original(original), "matchType": "exact", "output": "json",
             "fl": ",".join(CDX_FIELDS), "limit": str(limit), "showResumeKey": "true"}
    return f"https://{WAYBACK}/cdx/search/cdx?" + urllib.parse.urlencode(query)


def replay_url(timestamp, original):
    if not re.fullmatch(r"\d{14}", str(timestamp)):
        raise ValueError(f"bad capture timestamp {timestamp!r}")
    return f"https://{WAYBACK}/web/{timestamp}id_/{check_original(original)}"


# ---------------------------------------------------------------- CDX

def parse_cdx(data, original, limit=None):
    """Rows whose original equals the request exactly; others are rejected, truncation is flagged."""
    text = data.decode("utf-8").strip()
    table = json.loads(text) if text else []
    out = {"rows": [], "rejected": [], "resume_key": None, "truncated": False, "returned": 0}
    if not table:
        return out
    if tuple(table[0]) != CDX_FIELDS:
        raise ValueError(f"unexpected CDX fields {table[0]!r}")
    body = table[1:]
    # showResumeKey=true appends [] and [key] when more rows exist.
    if len(body) >= 2 and body[-2] == [] and len(body[-1]) == 1:
        out["resume_key"], body = body[-1][0], body[:-2]
    elif body and body[-1] == []:
        body = body[:-1]
    out["returned"] = len(body)
    for row in body:
        if not isinstance(row, list) or len(row) != len(CDX_FIELDS):
            out["rejected"].append({"row": row, "reason": "malformed_row"})
            continue
        rec = dict(zip(CDX_FIELDS, row))
        if rec["original"] != original:
            out["rejected"].append({"row": rec, "reason": "original_not_exact"})
        elif not re.fullmatch(r"\d{14}", rec["timestamp"]):
            out["rejected"].append({"row": rec, "reason": "bad_timestamp"})
        elif not re.fullmatch(r"[A-Z2-7]{32}", rec["digest"]):
            out["rejected"].append({"row": rec, "reason": "digest_not_sha1_base32"})
        else:
            out["rows"].append(rec)
    out["truncated"] = out["resume_key"] is not None or (limit is not None and out["returned"] >= limit)
    return out


def select_capture(rows, prefer=None):
    """One explicit capture: the preferred timestamp if present, else the latest status-200 row returned."""
    if not rows:
        return None, "cdx_zero_rows"
    ok = [r for r in rows if r["statuscode"] == "200"]
    for row in ok:
        if prefer and row["timestamp"] == prefer:
            return row, "preferred_timestamp"
    if prefer:
        return None, "preferred_capture_not_found"
    if not ok:
        return None, "no_status_200_capture"
    return max(ok, key=lambda r: r["timestamp"]), "latest_status_200_in_returned_rows"


# ---------------------------------------------------------------- verification

def decode_body(raw, content_encoding, max_decoded=LIMITS["decoded_bytes"]):
    """Return (decoded, problem).  Bounded gzip inflate; truncation, bombs and trailing bytes are errors."""
    enc = (content_encoding or "").strip().lower()
    magic = raw[:2] == b"\x1f\x8b"
    if enc in ("", "identity"):
        if len(raw) > max_decoded:
            return None, "decoded_too_large"
        return (None, "gzip_magic_without_content_encoding") if magic else (raw, None)
    if enc not in ("gzip", "x-gzip"):
        return None, f"unsupported_content_encoding:{enc}"
    if not magic:
        return None, "content_encoding_gzip_without_magic"
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(raw, max_decoded + 1)
    except zlib.error as err:
        return None, f"gzip_error:{err}"
    if len(out) > max_decoded or inflater.unconsumed_tail:
        return None, "decoded_too_large"
    if not inflater.eof:
        return None, "gzip_truncated"
    if inflater.unused_data:
        return None, "gzip_trailing_data"
    return out, None


def _memento_ts(value):
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def verify_capture(raw, headers, cdx_row, original, max_decoded=LIMITS["decoded_bytes"]):
    """Provenance record plus decoded bytes; status is 'verified' only when every check passes."""
    h = {k.lower(): v for k, v in headers.items()}
    rec = {"original": original, "capture": cdx_row["timestamp"], "cdx_row": dict(cdx_row),
           "replay_url": replay_url(cdx_row["timestamp"], original),
           "raw_bytes": len(raw), "raw_sha256": sha256_hex(raw), "raw_sha1_b32": sha1_b32(raw),
           "content_encoding": h.get("content-encoding"), "memento_datetime": h.get("memento-datetime"),
           "x_archive_src": h.get("x-archive-src"), "digest_basis": DIGEST_BASIS}
    problems = []
    if cdx_row.get("original") != original:
        problems.append("cdx_original_not_exact")
    if cdx_row.get("statuscode") != "200":
        problems.append("cdx_status_not_200")
    if f'<{original}>; rel="original"' not in h.get("link", ""):
        problems.append("link_rel_original_mismatch")
    if _memento_ts(h.get("memento-datetime")) != cdx_row["timestamp"]:
        problems.append("memento_datetime_mismatch")
    length = h.get("content-length")
    if length is not None and (not str(length).isdigit() or int(length) != len(raw)):
        problems.append("content_length_mismatch")
    decoded, decode_problem = decode_body(raw, h.get("content-encoding"), max_decoded)
    if decode_problem:
        problems.append(decode_problem)
    rec["digest_match"] = rec["raw_sha1_b32"] == cdx_row.get("digest")
    if not rec["digest_match"]:
        # A digest of decoded bytes is a different convention, not the same evidence.
        if decoded is not None and sha1_b32(decoded) == cdx_row.get("digest"):
            problems.append("digest_matches_decoded_not_raw")
        else:
            problems.append("digest_mismatch")
    if decoded is not None:
        rec["decoded_bytes"], rec["decoded_sha256"] = len(decoded), sha256_hex(decoded)
    rec["problems"] = problems
    rec["status"] = "verified" if not problems else "rejected"
    return rec, (decoded if not problems else None)


def check_main(doc, ticker, cik):
    """Identity and shape of a main submissions JSON; returns problem strings."""
    if not isinstance(doc, dict):
        return ["main_not_object"]
    problems = []
    raw_cik = doc.get("cik")
    if not (isinstance(raw_cik, (str, int)) and str(raw_cik).isdigit() and int(raw_cik) == int(cik)):
        problems.append(f"cik_mismatch:{raw_cik!r}")
    tickers = [t.upper() for t in doc.get("tickers") or [] if isinstance(t, str)]
    if ticker.upper() not in tickers:
        problems.append(f"ticker_missing:{tickers}")
    if EXPECTED_CIK.get(ticker.upper(), int(cik)) != int(cik):
        problems.append("cik_vs_pinned_mismatch")
    filings = doc.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict) \
            or not isinstance(filings.get("files", []), list):
        return problems + ["filings_shape"]
    problems.extend(problem.replace("shard_", "recent_", 1) for problem in check_shard(filings["recent"]))
    for entry in filings.get("files", []):
        try:
            shard_url(cik, entry.get("name") if isinstance(entry, dict) else None)
        except ValueError as err:
            problems.append(str(err))
    return problems


def check_shard(doc):
    if not isinstance(doc, dict):
        return ["shard_not_object"]
    missing = [k for k in ("accessionNumber", "filingDate", "form", "acceptanceDateTime")
               if not isinstance(doc.get(k), list)]
    if missing:
        return [f"shard_columns_missing:{missing}"]
    try:
        columnar_rows(doc)
    except ValueError as err:
        return [f"shard_ragged:{err}"]
    return []


# ---------------------------------------------------------------- http

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises HTTPError with the 3xx status; redirects are failures


class SerialClient:
    """One request at a time to web.archive.org, >= min_interval apart, stop on the first 403/429."""

    def __init__(self, store, opener=None, sleep=time.sleep, clock=time.monotonic,
                 min_interval=MIN_INTERVAL, limits=None, user_agent=USER_AGENT):
        self.store, self.sleep, self.clock = store, sleep, clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.min_interval, self.limits = max(MIN_INTERVAL, min_interval), dict(LIMITS, **(limits or {}))
        self.user_agent, self.last_done, self.stopped, self.requests = user_agent, None, {}, 0
        # A new CLI process must also respect the previous process's stop.
        if store.log_path.exists():
            for line in store.log_path.read_text().splitlines():
                event = json.loads(line)
                if event.get("event") == "host_stopped" and event.get("host") == WAYBACK:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(event["at"])).total_seconds()
                    if elapsed < 300:
                        self.stopped[WAYBACK] = event["status"]

    def _read(self, resp, max_bytes, start):
        chunks, total = [], 0
        while True:
            if self.clock() - start > self.limits["seconds"]:
                return b"".join(chunks), "deadline_exceeded_partial"
            try:
                chunk = resp.read(65536)
            except (TimeoutError, socket.timeout, http.client.IncompleteRead, OSError) as err:
                return b"".join(chunks), f"read_error_partial:{type(err).__name__}"
            if not chunk:
                return b"".join(chunks), None
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return b"".join(chunks), "body_too_large"

    def get(self, url, kind, max_bytes):
        host = urllib.parse.urlsplit(url).hostname
        if host != WAYBACK or not url.startswith(f"https://{WAYBACK}/"):
            raise ValueError(f"network fetches are limited to https://{WAYBACK}/: {url!r}")
        if host in self.stopped:
            raise HostStopped(f"{host} stopped after HTTP {self.stopped[host]}")
        if self.last_done is not None:
            delay = self.min_interval - (self.clock() - self.last_done)
            if delay > 0:
                self.sleep(delay)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
        status, headers, body, error, start = None, {}, b"", None, self.clock()
        self.requests += 1
        try:
            with self.opener(req, timeout=min(60.0, self.limits["seconds"])) as resp:
                status, headers = resp.status, dict(resp.headers)
                body, error = self._read(resp, max_bytes, start)
        except urllib.error.HTTPError as err:
            status, headers = err.code, dict(err.headers or {})
            if err.fp:
                body, _ = self._read(err, max_bytes, start)
            error = f"http_{err.code}"
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as err:
            error = f"{type(err).__name__}: {err}"
        finally:
            self.last_done = self.clock()
        h = {k.lower(): v for k, v in headers.items()}
        if error is None and h.get("content-length", "").isdigit() and int(h["content-length"]) != len(body):
            error = "content_length_mismatch_partial"
        ok = status == 200 and error is None
        rec = {"event": "fetch", "kind": kind, "url": url, "status": status, "ok": ok, "error": error,
               "bytes": len(body), "retrieved_at": now_utc(), "content_type": h.get("content-type"),
               "content_encoding": h.get("content-encoding"), "location": h.get("location")}
        if body:
            # Failure bodies (403 pages, partial reads) are kept as evidence, never as success.
            rec["sha256"] = self.store.put(body)
        self.store.append(self.store.log_path, rec)
        if status in (403, 429):
            self.stopped[host] = status
            self.store.append(self.store.log_path, {"event": "host_stopped", "host": host, "status": status,
                                                    "at": now_utc()})
        return rec, body, headers


# ---------------------------------------------------------------- captures

def captures_path(store):
    return store.root / "captures.jsonl"


def latest_captures(store):
    """Latest record per original URL (verified or not)."""
    latest, path = {}, captures_path(store)
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                latest[rec["original"]] = rec
    return latest


def load_verified(store, rec):
    """Re-check stored raw bytes against the recorded CDX digest and decode again."""
    if not rec or rec.get("status") != "verified":
        return None
    try:
        original = check_original(rec["original"])
        cdx = store.read(rec["cdx_response_sha256"])
        if cdx is None or rec["cdx_row"] not in parse_cdx(cdx, original)["rows"]:
            return None
        if rec["capture"] != rec["cdx_row"]["timestamp"]:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    raw = store.read(rec.get("raw_sha256") or "0" * 64) if rec and rec.get("status") == "verified" else None
    if raw is None or sha1_b32(raw) != rec["cdx_row"]["digest"]:
        return None
    decoded, problem = decode_body(raw, rec.get("content_encoding"))
    return None if problem or sha256_hex(decoded) != rec.get("decoded_sha256") else decoded


def _record(store, rec, raw, decoded):
    if raw:
        store.put(raw)
    if decoded is not None:
        store.put(decoded)
    rec["recorded_at"] = now_utc()
    store.append(captures_path(store), rec)
    return rec


def acquire(client, original, prefer=None, limit=CDX_LIMIT):
    """CDX discovery then id_ replay of one explicit capture; returns (record, decoded or None)."""
    query = cdx_url(original, limit)
    fetch, body, _ = client.get(query, "cdx", client.limits["cdx_bytes"])
    base = {"original": original, "acquired_via": "network", "cdx_query": query,
            "cdx_response_sha256": fetch.get("sha256"), "cdx_fetch": fetch}
    if not fetch["ok"]:
        return _record(client.store, dict(base, status="cdx_failed", problems=[fetch["error"]]), b"", None), None
    try:
        parsed = parse_cdx(body, original, limit)
    except ValueError as err:
        return _record(client.store, dict(base, status="cdx_failed", problems=[f"cdx_parse:{err}"]), b"", None), None
    base.update(cdx_returned=parsed["returned"], cdx_truncated=parsed["truncated"],
                cdx_resume_key=parsed["resume_key"], cdx_rejected_rows=parsed["rejected"],
                cdx_status_counts=_status_counts(parsed["rows"]))
    row, reason = select_capture(parsed["rows"], prefer)
    if row is None:
        return _record(client.store, dict(base, status="no_capture", problems=[reason]), b"", None), None
    replay, raw, headers = client.get(replay_url(row["timestamp"], original), "replay", client.limits["body_bytes"])
    if not replay["ok"]:
        rec = dict(base, capture=row["timestamp"], cdx_row=row, selection=reason, replay_fetch=replay,
                   status="replay_failed", problems=[replay["error"] or f"http_{replay['status']}"])
        return _record(client.store, rec, raw, None), None
    rec, decoded = verify_capture(raw, headers, row, original, client.limits["decoded_bytes"])
    rec = dict(base, **rec, selection=reason, replay_fetch=replay)
    return _record(client.store, rec, raw, decoded), decoded


def _status_counts(rows):
    counts = {}
    for row in rows:
        counts[row["statuscode"]] = counts.get(row["statuscode"], 0) + 1
    return counts


def parse_header_dump(text):
    """Last response block of a curl -D dump -> (status, headers)."""
    blocks = [b for b in re.split(r"\r?\n\r?\n", text) if b.strip().startswith("HTTP/")]
    if not blocks:
        raise ValueError("no HTTP response block")
    lines = blocks[-1].strip().splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers.setdefault(key.strip().lower(), value.strip())
    return status, headers


def import_capture(store, body_path, headers_path, cdx_path, original, timestamp, cdx_query=None,
                   max_decoded=LIMITS["decoded_bytes"]):
    """Register a previously downloaded capture after re-verifying it against its saved CDX row."""
    for path, limit in ((body_path, LIMITS["body_bytes"]), (cdx_path, LIMITS["cdx_bytes"]),
                        (headers_path, 65536)):
        if Path(path).stat().st_size > limit:
            raise ValueError("import file exceeds size limit")
    cdx_bytes = Path(cdx_path).read_bytes()
    limit = None
    if cdx_query:
        limit_value = urllib.parse.parse_qs(urllib.parse.urlsplit(cdx_query).query).get("limit", [None])[0]
        limit = int(limit_value) if limit_value and limit_value.lstrip("-").isdigit() else None
    base = {"original": check_original(original), "acquired_via": "import", "import_body": str(body_path),
            "import_cdx": str(cdx_path), "cdx_query": cdx_query, "cdx_response_sha256": store.put(cdx_bytes)}
    parsed = parse_cdx(cdx_bytes, original, limit)
    base.update(cdx_returned=parsed["returned"], cdx_truncated=parsed["truncated"],
                cdx_rejected_rows=parsed["rejected"], cdx_status_counts=_status_counts(parsed["rows"]))
    row = next((r for r in parsed["rows"] if r["timestamp"] == timestamp), None)
    if row is None:
        return _record(store, dict(base, capture=timestamp, status="rejected",
                                   problems=["capture_not_in_saved_cdx"]), b"", None), None
    status, headers = parse_header_dump(Path(headers_path).read_text(errors="replace"))
    raw = Path(body_path).read_bytes()
    rec, decoded = verify_capture(raw, headers, row, original, max_decoded)
    if status != 200:
        rec["problems"].append(f"saved_status_{status}")
        rec["status"], decoded = "rejected", None
    rec = dict(base, **rec, selection="imported_verified_sample")
    return _record(store, rec, raw, decoded), decoded


def import_batch06(store, source_dir, ciks):
    """Import wayback-submissions-<SYM>-CIK<cik10>-<ts>.json.body files with their saved CDX responses."""
    source_dir = Path(source_dir)
    queries = {}
    log = source_dir / "fetch-log.jsonl"
    if log.exists():
        for line in log.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                queries[entry["name"]] = entry.get("requested_url")
    results = []
    for symbol, cik in ciks.items():
        for body in sorted(source_dir.glob(f"wayback-submissions-{symbol}-CIK{cik10(cik)}-*.json.body")):
            timestamp = body.name.rsplit("-", 1)[1].split(".")[0]
            cdx_name = f"wayback-cdx-submissions-CIK{cik10(cik)}"
            rec, _ = import_capture(store, body, body.with_suffix(".headers"), source_dir / f"{cdx_name}.body",
                                    submissions_url(cik), timestamp, queries.get(cdx_name))
            results.append({"symbol": symbol, "original": rec["original"], "capture": rec.get("capture"),
                            "status": rec["status"], "problems": rec.get("problems")})
    return results


# ---------------------------------------------------------------- collect

def _decoded_json(decoded):
    try:
        return json.loads(decoded.decode("utf-8")), None
    except (UnicodeDecodeError, ValueError) as err:
        return None, f"json_error:{err}"


def _reject_content(store, rec, problems):
    rec = dict(rec, status="rejected_content", problems=list(rec.get("problems", [])) + problems)
    rec.pop("recorded_at", None)
    return _record(store, rec, b"", None)


def collect_symbol(client, symbol, cik, prefer_main=None, max_shards=None):
    """Main submissions plus every listed shard; stops (and reports) on host stop."""
    store, out = client.store, {"symbol": symbol, "cik": cik10(cik), "main": None, "shards": {}, "not_attempted": []}
    original = submissions_url(cik)
    rec = latest_captures(store).get(original)
    decoded = load_verified(store, rec)
    if decoded is None or (prefer_main and rec.get("capture") != prefer_main):
        rec, decoded = acquire(client, original, prefer_main)
    out["main"] = {"capture": rec.get("capture"), "status": rec["status"], "via": rec.get("acquired_via")}
    if decoded is None:
        return out
    doc, err = _decoded_json(decoded)
    problems = [err] if err else check_main(doc, symbol, cik)
    if problems:
        _reject_content(store, rec, problems)
        out["main"].update(status="rejected_content", problems=problems)
        return out
    names = [f["name"] for f in doc["filings"].get("files", [])]
    for i, name in enumerate(names):
        if max_shards is not None and i >= max_shards:
            out["not_attempted"] += [{"shard": n, "reason": "max_shards"} for n in names[i:]]
            break
        url = shard_url(cik, name)
        srec = latest_captures(store).get(url)
        sdecoded = load_verified(store, srec)
        try:
            if sdecoded is None:
                srec, sdecoded = acquire(client, url)
        except HostStopped as err:
            out["not_attempted"] += [{"shard": n, "reason": f"host_stopped: {err}"} for n in names[i:]]
            break
        out["shards"][name] = {"capture": srec.get("capture"), "status": srec["status"],
                               "problems": srec.get("problems")}
        if sdecoded is not None:
            sdoc, err = _decoded_json(sdecoded)
            sproblems = [err] if err else check_shard(sdoc)
            if sproblems:
                _reject_content(store, srec, sproblems)
                out["shards"][name].update(status="rejected_content", problems=sproblems)
        if client.stopped:
            out["not_attempted"] += [{"shard": n, "reason": "host_stopped"} for n in names[i + 1:]]
            break
    return out


# ---------------------------------------------------------------- index

def _verified_doc(store, captures, original):
    decoded = load_verified(store, captures.get(original))
    return _decoded_json(decoded)[0] if decoded is not None else None


def build_index(store, symbol, cik, through):
    """Merged filings index filtered to filingDate <= through, plus a gap list.  Raw fields only."""
    captures = latest_captures(store)
    gaps = []
    main_url = submissions_url(cik)
    main = _verified_doc(store, captures, main_url)
    if main is None or check_main(main, symbol, cik):
        rec = captures.get(main_url) or {}
        gaps.append({"kind": "main_unavailable", "status": rec.get("status"), "problems": rec.get("problems"),
                     "severity": "blocking"})
        return None, gaps
    main_capture = captures[main_url]["capture"]
    if main_capture[:8] <= through.replace("-", ""):
        gaps.append({"kind": "main_capture_not_after_through", "capture": main_capture, "through": through,
                     "severity": "gap"})
    shards = {}
    for entry in main["filings"].get("files", []):
        url = shard_url(cik, entry["name"])
        doc = _verified_doc(store, captures, url)
        if doc is not None and not check_shard(doc):
            shards[entry["name"]] = doc
        else:
            rec = captures.get(url) or {}
            gaps.append({"kind": "shard_unverified", "shard": entry["name"], "status": rec.get("status", "absent"),
                         "problems": rec.get("problems"), "severity": "gap"})
    rows, findings = merge_submissions(main, shards)
    gaps += [dict(f, kind=f["check"]) for f in findings]
    for name, rec in ((n, captures.get(shard_url(cik, n), {})) for n in shards):
        if rec.get("cdx_truncated"):
            gaps.append({"kind": "cdx_truncated_selection", "shard": name, "severity": "note"})
    if captures[main_url].get("cdx_truncated"):
        gaps.append({"kind": "cdx_truncated_selection", "shard": "main", "severity": "note"})
    kept = [r for r in rows if r["filingDate"] <= through]
    counts = {"plan_forms": {}, "form_variants": {}, "other_forms": {}}
    for row in kept:
        form = row["form"]
        bucket = "plan_forms" if form in PLAN_FORMS else "form_variants" if form in FORM_VARIANTS else "other_forms"
        counts[bucket][form] = counts[bucket].get(form, 0) + 1
    filings = [dict(r, available_at=None, acceptance_time_status="raw_unverified") for r in kept]
    sources = {name: {"capture": captures[shard_url(cik, name)]["capture"],
                      "raw_sha256": captures[shard_url(cik, name)]["raw_sha256"]} for name in shards}
    index = {"symbol": symbol, "cik": cik10(cik), "through": through,
             "benchmark_ready": False,
             "index_complete_relative_to_selected_main": not any(
                 gap.get("severity") in ("gap", "error", "blocking") for gap in gaps),
             "main": {"original": main_url, "capture": main_capture,
                      "raw_sha256": captures[main_url]["raw_sha256"],
                      "decoded_sha256": captures[main_url].get("decoded_sha256")},
             "shards": sources, "source_note": "Internet Archive copy of SEC data; not SEC direct",
             "merged_total": len(rows), "kept": len(kept), "excluded_after_through": len(rows) - len(kept),
             "first_filing": kept[0]["filingDate"] if kept else None,
             "last_filing": kept[-1]["filingDate"] if kept else None,
             "counts": counts, "filings": filings}
    return index, gaps


# ---------------------------------------------------------------- CLI

def parse_symbols(values):
    """AAPL or FOO=123456; bare symbols must be in EXPECTED_CIK."""
    out = {}
    for value in values:
        symbol, _, cik = value.partition("=")
        symbol = symbol.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", symbol):
            raise ValueError("invalid symbol")
        if cik:
            if not cik.isdigit() or not 0 < int(cik) < 10**10:
                raise ValueError("invalid CIK")
            if symbol in EXPECTED_CIK and int(cik) != EXPECTED_CIK[symbol]:
                raise ValueError("CIK disagrees with pinned identity")
            out[symbol] = int(cik)
        elif symbol in EXPECTED_CIK:
            out[symbol] = EXPECTED_CIK[symbol]
        else:
            raise SystemExit(f"{symbol}: give SYMBOL=CIK for symbols outside EXPECTED_CIK")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["import", "collect", "index"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--source-dir", type=Path, help="batch-06 sources/ directory for import")
    parser.add_argument("--suite", type=Path, default=Path("benchmarks/suites/nasdaq-diagnostic-v1.toml"))
    parser.add_argument("--through", help="override the suite's latest terminal session")
    parser.add_argument("--max-shards", type=int, help="cap shard downloads per symbol this run")
    parser.add_argument("--prefer-main", help="require this exact 14-digit main capture; use one symbol")
    args = parser.parse_args(argv)
    if args.max_shards is not None and args.max_shards < 0:
        parser.error("--max-shards must be nonnegative")
    if args.command == "import" and not args.source_dir:
        parser.error("import requires --source-dir")
    store, ciks = Store(args.root), parse_symbols(args.symbols)
    if args.prefer_main and (len(ciks) != 1 or not re.fullmatch(r"\d{14}", args.prefer_main)):
        parser.error("--prefer-main requires one symbol and a 14-digit capture timestamp")
    if args.command == "import":
        result = {"imported": import_batch06(store, args.source_dir, ciks)}
    elif args.command == "collect":
        client, result = SerialClient(store), {"symbols": []}
        for symbol, cik in ciks.items():
            try:
                result["symbols"].append(collect_symbol(client, symbol, cik, prefer_main=args.prefer_main,
                                                        max_shards=args.max_shards))
            except HostStopped as err:
                result["symbols"].append({"symbol": symbol, "not_attempted": f"host_stopped: {err}"})
        result.update(requests=client.requests, stopped=client.stopped)
    else:
        from scripts.archive_bounds import suite_bounds
        bounds = suite_bounds(args.suite)
        through = date.fromisoformat(args.through or bounds["through"]).isoformat()
        result = {"through": through, "through_source": "--through" if args.through else "suite max terminal_session",
                  "symbols": {}, "gaps": {}}
        (store.root / "index").mkdir(exist_ok=True)
        for symbol, cik in ciks.items():
            index, gaps = build_index(store, symbol, cik, through)
            result["gaps"][symbol] = gaps
            if index:
                (store.root / "index" / f"{symbol}.json").write_text(json.dumps(index, indent=1) + "\n")
                result["symbols"][symbol] = {k: index[k] for k in ("main", "shards", "merged_total", "kept",
                                                                   "excluded_after_through", "first_filing",
                                                                   "last_filing", "counts")}
        (store.root / "gaps.json").write_text(json.dumps(result["gaps"], indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
