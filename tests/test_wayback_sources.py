"""Offline tests for scripts/wayback_sources.py (digest, gzip bounds, identity, URL/path, 403 stop, shards).

Runs under pytest or `python -m unittest tests.test_wayback_sources`.
"""
import gzip
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wayback_sources", ROOT / "scripts" / "wayback_sources.py")
wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb)

CIK = 320193
MAIN = "https://data.sec.gov/submissions/CIK0000320193.json"
SHARD_NAME = "CIK0000320193-submissions-001.json"
SHARD = "https://data.sec.gov/submissions/" + SHARD_NAME
TS = "20260102013624"
MEMENTO = "Fri, 02 Jan 2026 01:36:24 GMT"


def cols(rows):
    keys = ("accessionNumber", "filingDate", "form", "acceptanceDateTime")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


RECENT = [("0000320193-23-000077", "2023-08-04", "10-Q", "2023-08-03T18:04:43.000Z"),
          ("0000320193-24-000123", "2024-11-01", "10-K", "2024-11-01T06:01:36.000Z")]
OLD = [("0000320193-05-000001", "2005-01-10", "10-K405", "2005-01-10T12:00:00.000Z"),
       ("0000320193-10-000002", "2010-02-01", "10-Q", "2010-02-01T12:00:00.000Z")]


def main_doc(files=None, cik="0000320193", tickers=("AAPL",)):
    if files is None:
        files = [{"name": SHARD_NAME, "filingCount": 2, "filingFrom": "2005-01-10", "filingTo": "2010-02-01"}]
    return {"cik": cik, "tickers": list(tickers), "filings": {"recent": cols(RECENT), "files": files}}


def gz(obj):
    return gzip.compress(json.dumps(obj).encode(), mtime=0)


def headers_for(original, raw, ts=TS, memento=MEMENTO, encoding="gzip"):
    h = {"Content-Type": "application/json", "Content-Length": str(len(raw)), "Memento-Datetime": memento,
         "Link": f'<{original}>; rel="original", <https://web.archive.org/web/timemap/link/{original}>; rel="timemap"'}
    if encoding:
        h["Content-Encoding"] = encoding
    return h


def cdx_body(original, rows, resume=None):
    table = [list(wb.CDX_FIELDS)] + [[ts, orig, "application/json", st, dg, "100"] for ts, orig, st, dg in rows]
    if resume:
        table += [[], [resume]]
    return json.dumps(table).encode()


class FakeResponse:
    def __init__(self, status, body, headers=None, fail_after=None):
        self.status, self.headers = status, headers or {}
        self._buf, self._fail_after, self._sent = io.BytesIO(body), fail_after, 0

    def read(self, n=-1):
        if self._fail_after is not None and self._sent >= self._fail_after:
            raise TimeoutError("timed out")
        chunk = self._buf.read(n if self._fail_after is None else min(n, self._fail_after - self._sent))
        self._sent += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWayback:
    """Serves CDX and id_ replays from a dict of original -> (raw, headers); records every URL."""

    def __init__(self, captures, statuses=None):
        self.captures, self.statuses, self.urls = captures, statuses or {}, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        for marker, code in self.statuses.items():
            if marker in url:
                raise urllib.error.HTTPError(url, code, "blocked", {}, io.BytesIO(b"<html>blocked</html>"))
        if "/cdx/search/cdx?" in url:
            original = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["url"][0]
            if original not in self.captures:
                return FakeResponse(200, json.dumps([list(wb.CDX_FIELDS)]).encode())
            raw, _ = self.captures[original]
            return FakeResponse(200, cdx_body(original, [(TS, original, "200", wb.sha1_b32(raw))]))
        original = url.split("id_/", 1)[1]
        raw, headers = self.captures[original]
        return FakeResponse(200, raw, headers)


import urllib.parse  # noqa: E402  (used by FakeWayback)


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(tmp, opener, **kw):
    clock = Clock()
    store = wb.Store(Path(tmp))
    return wb.SerialClient(store, opener=opener, sleep=clock.sleep, clock=clock, **kw), clock


class VerifyCaptureTests(unittest.TestCase):
    def setUp(self):
        self.raw = gz(main_doc())
        self.row = {"timestamp": TS, "original": MAIN, "mimetype": "application/json", "statuscode": "200",
                    "digest": wb.sha1_b32(self.raw), "length": "100"}

    def test_raw_digest_match_is_verified_and_hashes_kept_separately(self):
        rec, decoded = wb.verify_capture(self.raw, headers_for(MAIN, self.raw), self.row, MAIN)
        self.assertEqual(rec["status"], "verified", rec["problems"])
        self.assertTrue(rec["digest_match"])
        self.assertNotEqual(rec["raw_sha256"], rec["decoded_sha256"])
        self.assertEqual(json.loads(decoded)["cik"], "0000320193")

    def test_digest_mismatch_rejected(self):
        row = dict(self.row, digest="A" * 32)
        rec, decoded = wb.verify_capture(self.raw, headers_for(MAIN, self.raw), row, MAIN)
        self.assertEqual(rec["status"], "rejected")
        self.assertIn("digest_mismatch", rec["problems"])
        self.assertIsNone(decoded)

    def test_decoded_digest_is_a_different_convention_and_rejected(self):
        decoded = gzip.decompress(self.raw)
        row = dict(self.row, digest=wb.sha1_b32(decoded))
        rec, out = wb.verify_capture(self.raw, headers_for(MAIN, self.raw), row, MAIN)
        self.assertIn("digest_matches_decoded_not_raw", rec["problems"])
        self.assertIsNone(out)

    def test_memento_and_link_must_match_capture_and_original(self):
        h = headers_for(MAIN, self.raw, memento="Fri, 02 Jan 2026 01:36:25 GMT")
        h["Link"] = '<https://data.sec.gov/submissions/CIK0001045810.json>; rel="original"'
        rec, _ = wb.verify_capture(self.raw, h, self.row, MAIN)
        self.assertIn("memento_datetime_mismatch", rec["problems"])
        self.assertIn("link_rel_original_mismatch", rec["problems"])

    def test_cdx_row_for_other_original_rejected(self):
        rec, _ = wb.verify_capture(self.raw, headers_for(MAIN, self.raw), dict(self.row, original=SHARD), MAIN)
        self.assertIn("cdx_original_not_exact", rec["problems"])


class GzipBoundsTests(unittest.TestCase):
    def test_identity_response_also_obeys_decoded_limit(self):
        self.assertEqual(wb.decode_body(b"x" * 101, "identity", max_decoded=100),
                         (None, "decoded_too_large"))

    def test_truncated_gzip(self):
        raw = gz({"x": "y" * 5000})
        _, problem = wb.decode_body(raw[:-12], "gzip")
        self.assertIn(problem, ("gzip_truncated",))

    def test_gzip_bomb_capped(self):
        raw = gzip.compress(b"\0" * 5_000_000)
        self.assertLess(len(raw), 10_000)
        out, problem = wb.decode_body(raw, "gzip", max_decoded=1_000_000)
        self.assertIsNone(out)
        self.assertEqual(problem, "decoded_too_large")

    def test_trailing_bytes_and_encoding_disagreements(self):
        raw = gz({"a": 1})
        self.assertEqual(wb.decode_body(raw + b"junk", "gzip")[1], "gzip_trailing_data")
        self.assertEqual(wb.decode_body(raw, None)[1], "gzip_magic_without_content_encoding")
        self.assertEqual(wb.decode_body(b"{}", "gzip")[1], "content_encoding_gzip_without_magic")
        self.assertEqual(wb.decode_body(b"{}", "br")[1], "unsupported_content_encoding:br")
        self.assertEqual(wb.decode_body(b"{}", None), (b"{}", None))


class UrlAndIdentityTests(unittest.TestCase):
    def test_symbol_cannot_escape_output_directory_or_override_pinned_cik(self):
        for value in ("../../escape=123", "AAPL=1", "X=-1", "X=10000000000"):
            with self.assertRaises(ValueError):
                wb.parse_symbols([value])
        self.assertTrue(wb.check_main({"cik": CIK, "tickers": ["AAPL"],
                                       "filings": {"recent": {}, "files": []}}, "AAPL", CIK))

    def test_shard_names_only_same_cik_no_traversal(self):
        self.assertEqual(wb.shard_url(CIK, SHARD_NAME), SHARD)
        for bad in ("../CIK0000320193-submissions-001.json", "CIK0000320193-submissions-001.json/..",
                    "CIK0001045810-submissions-001.json", "CIK0000320193-submissions-1.json",
                    "sub/CIK0000320193-submissions-001.json", "CIK0000320193-submissions-001.json?x=1", None):
            with self.assertRaises(ValueError, msg=bad):
                wb.shard_url(CIK, bad)

    def test_originals_restricted_to_sec_https(self):
        for bad in ("http://data.sec.gov/submissions/CIK0000320193.json",
                    "https://data.sec.gov.evil.com/submissions/x.json",
                    "https://data.sec.gov:8443/submissions/x.json",
                    "https://user@data.sec.gov/submissions/x.json",
                    "https://www.sec.gov/Archives/edgar/data/../x",
                    "https://www.sec.gov/Archives/%2e%2e/x"):
            with self.assertRaises(ValueError, msg=bad):
                wb.check_original(bad)

    def test_cdx_rejects_non_exact_originals_and_flags_truncation(self):
        body = cdx_body(MAIN, [(TS, MAIN, "200", "A" * 32),
                               ("20250101000000", "http://data.sec.gov/submissions/CIK0000320193.json", "200", "B" * 32),
                               ("20250101000001", MAIN + "?x", "200", "C" * 32),
                               ("20250101000002", MAIN, "200", "bad")], resume="abc")
        parsed = wb.parse_cdx(body, MAIN, limit=100)
        self.assertEqual([r["timestamp"] for r in parsed["rows"]], [TS])
        self.assertEqual(len(parsed["rejected"]), 3)
        self.assertTrue(parsed["truncated"])
        full = wb.parse_cdx(cdx_body(MAIN, [(TS, MAIN, "200", "A" * 32)] * 3), MAIN, limit=3)
        self.assertTrue(full["truncated"], "a page filled to the limit cannot be called complete")

    def test_select_capture_ignores_non_200(self):
        rows = [{"timestamp": "20260501000000", "statuscode": "403"}, {"timestamp": TS, "statuscode": "200"}]
        self.assertEqual(wb.select_capture(rows)[0]["timestamp"], TS)
        self.assertEqual(wb.select_capture(rows[:1]), (None, "no_status_200_capture"))
        self.assertEqual(wb.select_capture([]), (None, "cdx_zero_rows"))

    def test_main_identity(self):
        self.assertEqual(wb.check_main(main_doc(), "AAPL", CIK), [])
        self.assertTrue(any(p.startswith("cik_mismatch") for p in wb.check_main(main_doc(cik="0001045810"), "AAPL", CIK)))
        self.assertTrue(any(p.startswith("ticker_missing") for p in wb.check_main(main_doc(tickers=("NVDA",)), "AAPL", CIK)))
        self.assertIn("cik_vs_pinned_mismatch", wb.check_main(main_doc(cik="0000000001"), "AAPL", 1))
        bad_files = main_doc(files=[{"name": "../../etc/passwd"}])
        self.assertTrue(any("shard name rejected" in p for p in wb.check_main(bad_files, "AAPL", CIK)))


class ClientTests(unittest.TestCase):
    def test_stop_survives_new_client_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, FakeWayback({}, statuses={"cdx": 403}))
            client.get(wb.cdx_url(MAIN), "cdx", 1000)
            fake = FakeWayback({})
            fresh, _ = make_client(tmp, fake)
            with self.assertRaises(wb.HostStopped):
                fresh.get(wb.cdx_url(MAIN), "cdx", 1000)
            self.assertEqual(fake.urls, [])

    def test_first_403_stops_host_and_keeps_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeWayback({}, statuses={"cdx": 403})
            client, _ = make_client(tmp, fake)
            rec, _, _ = client.get(wb.cdx_url(MAIN), "cdx", 1000)
            self.assertFalse(rec["ok"])
            self.assertEqual(client.store.read(rec["sha256"]), b"<html>blocked</html>")
            with self.assertRaises(wb.HostStopped):
                client.get(wb.cdx_url(SHARD), "cdx", 1000)
            self.assertEqual(len(fake.urls), 1, "no request after the first 403")

    def test_429_also_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, FakeWayback({}, statuses={"cdx": 429}))
            client.get(wb.cdx_url(MAIN), "cdx", 1000)
            self.assertEqual(client.stopped, {"web.archive.org": 429})

    def test_global_interval_and_host_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, clock = make_client(tmp, FakeWayback({}), min_interval=0.5)
            client.get(wb.cdx_url(MAIN), "cdx", 1000)
            client.get(wb.cdx_url(SHARD), "cdx", 1000)
            self.assertEqual(clock.sleeps, [2.0], "min interval cannot be lowered below 2 s")
            for url in (MAIN, "https://web.archive.org.evil.com/x", "http://web.archive.org/x"):
                with self.assertRaises(ValueError):
                    client.get(url, "x", 10)

    def test_partial_and_oversize_bodies_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, lambda req, timeout=None: FakeResponse(200, b"x" * 1000, fail_after=300))
            rec, body, _ = client.get(wb.cdx_url(MAIN), "cdx", 10_000)
            self.assertFalse(rec["ok"])
            self.assertTrue(rec["error"].startswith("read_error_partial"))
            self.assertEqual(len(body), 300)
            client.opener = lambda req, timeout=None: FakeResponse(200, b"x" * 1000)
            rec, _, _ = client.get(wb.cdx_url(MAIN), "cdx", 100)
            self.assertEqual(rec["error"], "body_too_large")
            client.opener = lambda req, timeout=None: FakeResponse(200, b"x" * 10, {"Content-Length": "50"})
            rec, _, _ = client.get(wb.cdx_url(MAIN), "cdx", 100)
            self.assertEqual(rec["error"], "content_length_mismatch_partial")

    def test_redirect_is_failure(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://example.com/"}, None)
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, opener)
            rec, _, _ = client.get(wb.cdx_url(MAIN), "cdx", 100)
            self.assertFalse(rec["ok"])
            self.assertEqual(rec["status"], 302)


def shard_doc(rows=OLD):
    return cols(rows)


class CollectAndIndexTests(unittest.TestCase):
    def test_explicit_capture_is_not_silently_replaced_by_cached_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), shard_doc()))
            wb.collect_symbol(client, "AAPL", CIK)
            previous = len(client.opener.urls)
            result = wb.collect_symbol(client, "AAPL", CIK, prefer_main="20000101000000")
            self.assertEqual(len(client.opener.urls), previous + 1)
            self.assertEqual(result["main"]["status"], "no_capture")
            self.assertEqual(wb.latest_captures(client.store)[MAIN]["problems"], ["preferred_capture_not_found"])

    def test_stored_capture_requires_actual_cdx_row_and_decoded_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), shard_doc()))
            wb.collect_symbol(client, "AAPL", CIK)
            rec = wb.latest_captures(client.store)[MAIN]
            for changes in ({"capture": "20000101000000"}, {"decoded_sha256": "0" * 64},
                            {"cdx_response_sha256": "0" * 64}):
                self.assertIsNone(wb.load_verified(client.store, dict(rec, **changes)))

    def serve(self, main=None, shard=None):
        captures = {}
        if main is not None:
            raw = gz(main)
            captures[MAIN] = (raw, headers_for(MAIN, raw))
        if shard is not None:
            raw = gz(shard)
            captures[SHARD] = (raw, headers_for(SHARD, raw))
        return FakeWayback(captures)

    def test_full_collect_and_index_filters_by_through_and_counts_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), shard_doc()))
            out = wb.collect_symbol(client, "AAPL", CIK)
            self.assertEqual(out["main"]["status"], "verified")
            self.assertEqual(out["shards"][SHARD_NAME]["status"], "verified")
            index, gaps = wb.build_index(client.store, "AAPL", CIK, "2024-10-04")
            self.assertEqual(index["merged_total"], 4)
            self.assertEqual(index["kept"], 3)
            self.assertEqual(index["excluded_after_through"], 1)
            self.assertEqual(index["counts"]["form_variants"], {"10-K405": 1})
            self.assertTrue(all(f["available_at"] is None for f in index["filings"]))
            self.assertEqual(index["filings"][-1]["acceptanceDateTime"], "2023-08-03T18:04:43.000Z")
            self.assertEqual([g for g in gaps if g["severity"] != "note"], [])
            # Second run reuses verified bytes without network.
            before = len(client.opener.urls)
            wb.collect_symbol(client, "AAPL", CIK)
            self.assertEqual(len(client.opener.urls), before)

    def test_missing_shard_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), None))
            out = wb.collect_symbol(client, "AAPL", CIK)
            self.assertEqual(out["shards"][SHARD_NAME]["status"], "no_capture")
            index, gaps = wb.build_index(client.store, "AAPL", CIK, "2024-10-04")
            kinds = {g["kind"] for g in gaps}
            self.assertIn("shard_unverified", kinds)
            self.assertIn("shard_missing", kinds)
            self.assertEqual(index["kept"], 1, "recent rows only, with the gap stated")

    def test_count_mismatch_and_duplicate_conflict(self):
        conflicting = OLD + [("0000320193-23-000077", "2023-08-05", "10-Q", "2023-08-03T18:04:43.000Z")]
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), shard_doc(conflicting)))
            wb.collect_symbol(client, "AAPL", CIK)
            _, gaps = wb.build_index(client.store, "AAPL", CIK, "2024-10-04")
            kinds = {g["kind"] for g in gaps}
            self.assertIn("shard_count_mismatch", kinds)
            self.assertIn("duplicate_conflict", kinds)
            self.assertIn("shard_date_out_of_range", kinds)

    def test_wrong_identity_main_rejected_and_no_shards_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self.serve(main_doc(cik="0001045810", tickers=("NVDA",)), shard_doc())
            client, _ = make_client(tmp, fake)
            out = wb.collect_symbol(client, "AAPL", CIK)
            self.assertEqual(out["main"]["status"], "rejected_content")
            self.assertFalse(any(SHARD_NAME in u for u in fake.urls))
            index, gaps = wb.build_index(client.store, "AAPL", CIK, "2024-10-04")
            self.assertIsNone(index)
            self.assertEqual(gaps[0]["kind"], "main_unavailable")

    def test_403_mid_run_marks_remaining_shards_not_attempted(self):
        files = [{"name": SHARD_NAME}, {"name": "CIK0000320193-submissions-002.json"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = self.serve(main_doc(files=files), shard_doc())
            fake.statuses = {"submissions-001": 403}
            client, _ = make_client(tmp, fake)
            out = wb.collect_symbol(client, "AAPL", CIK)
            self.assertEqual(out["shards"][SHARD_NAME]["status"], "cdx_failed")
            self.assertEqual([n["shard"] for n in out["not_attempted"]], ["CIK0000320193-submissions-002.json"])

    def test_stored_bytes_tampered_are_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, _ = make_client(tmp, self.serve(main_doc(), shard_doc()))
            wb.collect_symbol(client, "AAPL", CIK)
            rec = wb.latest_captures(client.store)[MAIN]
            client.store.blob_path(rec["raw_sha256"]).write_bytes(b"tampered")
            self.assertIsNone(wb.load_verified(client.store, rec))


class ImportTests(unittest.TestCase):
    def write(self, tmp, raw, cdx_rows, header_text):
        src = Path(tmp) / "src"
        src.mkdir()
        (src / "body").write_bytes(raw)
        (src / "headers").write_text(header_text)
        (src / "cdx").write_bytes(cdx_body(MAIN, cdx_rows))
        return src

    def header_text(self, raw, status=200):
        h = headers_for(MAIN, raw)
        return f"HTTP/2 {status} \r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"

    def test_import_verifies_against_saved_cdx_row(self):
        raw = gz(main_doc())
        with tempfile.TemporaryDirectory() as tmp:
            src = self.write(tmp, raw, [(TS, MAIN, "200", wb.sha1_b32(raw))], self.header_text(raw))
            store = wb.Store(Path(tmp) / "store")
            rec, decoded = wb.import_capture(store, src / "body", src / "headers", src / "cdx", MAIN, TS,
                                             "https://web.archive.org/cdx/search/cdx?url=x&limit=50")
            self.assertEqual(rec["status"], "verified", rec["problems"])
            self.assertIsNotNone(decoded)
            self.assertIsNotNone(wb.load_verified(store, wb.latest_captures(store)[MAIN]))

    def test_import_rejects_missing_row_digest_and_status(self):
        raw = gz(main_doc())
        with tempfile.TemporaryDirectory() as tmp:
            src = self.write(tmp, raw, [("20250101000000", MAIN, "200", wb.sha1_b32(raw))], self.header_text(raw))
            store = wb.Store(Path(tmp) / "store")
            rec, _ = wb.import_capture(store, src / "body", src / "headers", src / "cdx", MAIN, TS)
            self.assertEqual(rec["problems"], ["capture_not_in_saved_cdx"])
        with tempfile.TemporaryDirectory() as tmp:
            src = self.write(tmp, raw[:-5], [(TS, MAIN, "200", wb.sha1_b32(raw))], self.header_text(raw))
            rec, decoded = wb.import_capture(wb.Store(Path(tmp) / "s"), src / "body", src / "headers", src / "cdx",
                                             MAIN, TS)
            self.assertEqual(rec["status"], "rejected")
            self.assertIn("digest_mismatch", rec["problems"])
            self.assertIsNone(decoded)


if __name__ == "__main__":
    unittest.main()
