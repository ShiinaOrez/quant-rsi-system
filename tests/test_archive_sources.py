"""Boundary tests for scripts/archive_sources.py (403, retry, shards, accepted_at, cache integrity).

Runs under pytest or `python -m unittest tests.test_archive_sources`.
"""
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("archive_sources", ROOT / "scripts" / "archive_sources.py")
arch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arch)


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def scripted_opener(script):
    """Each call pops the next (status, body, headers) or raises the given exception."""
    calls = []

    def opener(req, timeout):
        calls.append((req.full_url, dict(req.header_items()), timeout))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "err", headers, io.BytesIO(body))
        return FakeResponse(status, body, headers)
    return opener, calls


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def make_fetcher(tmp, script, **kw):
    clock = Clock()
    opener, calls = scripted_opener(script)
    store = arch.Store(tmp)
    return arch.Fetcher(store, opener=opener, sleep=clock.sleep, clock=clock, **kw), calls, clock


class FetcherTests(unittest.TestCase):
    def test_403_is_logged_with_body_and_trips_breaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = b"<html>Undeclared Automated Tool</html>"
            f, calls, _ = make_fetcher(tmp, [(403, page, {})] * 2, max_consecutive_403=2)
            url = "https://data.sec.gov/submissions/CIK0000050863.json"
            r1 = f.get("a", url)
            r2 = f.get("b", url)
            self.assertEqual((r1["status"], r1["ok"]), (403, False))
            self.assertEqual(f.store.read(r1["sha256"]), page)  # evidence kept
            self.assertIn("data.sec.gov", f.blocked)
            with self.assertRaises(arch.HostBlocked):
                f.get("c", url)
            self.assertEqual(len(calls), 2)  # third request never sent
            log = [json.loads(l) for l in f.store.log_path.read_text().splitlines()]
            self.assertEqual([r["key"] for r in log], ["a", "b", "c"])
            self.assertEqual(log[2]["error"], "host_blocked_this_run")
            self.assertEqual(f.store.catalog(), {})  # failures never enter the catalog
            self.assertEqual(set(f.store.last_failures()), {"a", "b", "c"})

    def test_retry_on_503_and_timeout_honours_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = [(503, b"", {"Retry-After": "7"}), TimeoutError("read timed out"), (200, b"ok", {})]
            f, calls, clock = make_fetcher(tmp, script, retries=3)
            rec = f.get("k", "https://www.sec.gov/x")
            self.assertTrue(rec["ok"])
            self.assertEqual(rec["attempts"], 3)
            self.assertIn(7.0, clock.sleeps)
            self.assertEqual(len(calls), 3)

    def test_retries_exhausted_recorded_not_cataloged(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, _ = make_fetcher(tmp, [(429, b"", {})] * 3, retries=2)
            rec = f.get("k", "https://www.sec.gov/x")
            self.assertEqual((rec["status"], rec["attempts"], len(calls)), (429, 3, 3))
            self.assertNotIn("k", f.store.catalog())

    def test_rate_limit_spacing_per_host_and_user_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, clock = make_fetcher(tmp, [(200, b"1", {}), (200, b"2", {})], user_agent="ua-x (+url)")
            f.get("a", "https://www.sec.gov/a")
            f.get("b", "https://www.sec.gov/b")
            self.assertAlmostEqual(sum(clock.sleeps), 0.25)
            self.assertEqual(calls[0][1]["User-agent"], "ua-x (+url)")

    def test_cache_hit_and_corruption_triggers_refetch_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, calls, _ = make_fetcher(tmp, [(200, b"v1", {}), (200, b"v1", {}), (200, b"v2", {})])
            first = f.get("k", "https://www.sec.gov/a")
            self.assertTrue(f.get("k", "https://www.sec.gov/a")["cache_hit"])
            self.assertEqual(len(calls), 1)
            f.store.blob_path(first["sha256"]).write_bytes(b"tampered")
            again = f.get("k", "https://www.sec.gov/a")
            self.assertFalse(again["cache_hit"])
            self.assertEqual(f.store.read(again["sha256"]), b"v1")
            self.assertTrue(any((Path(tmp) / "quarantine").iterdir()))
            newer = f.get("k", "https://www.sec.gov/a", refresh=True)
            versions = f.store.versions("k")
            self.assertEqual(len(versions), 3)
            self.assertNotEqual(versions[0]["sha256"], newer["sha256"])
            self.assertEqual(f.store.read(versions[0]["sha256"]), b"v1")  # old version kept


class SecTests(unittest.TestCase):
    def main_json(self, recent, files):
        return {"tickers": ["INTC"], "filings": {"recent": recent, "files": files}}

    def cols(self, rows):
        keys = rows[0].keys()
        return {k: [r[k] for r in rows] for k in keys}

    def row(self, acc, form, date, accepted="2024-01-25T16:01:02.000Z"):
        return {"accessionNumber": acc, "form": form, "filingDate": date, "acceptanceDateTime": accepted}

    def test_merge_shards_dedupe_and_findings(self):
        recent = self.cols([self.row("0000050863-24-000010", "10-K", "2024-01-26"),
                            self.row("0000050863-19-000001", "8-K", "2019-03-14")])
        shard = self.cols([self.row("0000050863-19-000001", "8-K", "2019-03-14"),
                           self.row("0000050863-10-000002", "10-Q", "2010-05-01")])
        files = [{"name": "s-001.json", "filingCount": 2, "filingFrom": "2007-04-23", "filingTo": "2019-03-14"},
                 {"name": "s-002.json", "filingCount": 974, "filingFrom": "1994-02-10", "filingTo": "2007-04-22"}]
        rows, findings = arch.merge_submissions(self.main_json(recent, files), {"s-001.json": shard})
        self.assertEqual([r["accessionNumber"] for r in rows],
                         ["0000050863-10-000002", "0000050863-19-000001", "0000050863-24-000010"])
        self.assertEqual([f["check"] for f in findings], ["shard_missing"])

    def test_merge_detects_conflicts_and_count_mismatch(self):
        recent = self.cols([self.row("0000050863-19-000001", "8-K", "2019-03-14")])
        shard = self.cols([self.row("0000050863-19-000001", "8-K/A", "2019-03-14")])
        files = [{"name": "s.json", "filingCount": 5, "filingFrom": "2019-01-01", "filingTo": "2019-12-31"}]
        _, findings = arch.merge_submissions(self.main_json(recent, files), {"s.json": shard})
        checks = {f["check"] for f in findings}
        self.assertEqual(checks, {"shard_count_mismatch", "duplicate_conflict"})

    def test_resolve_cik_detects_mismatch_and_missing(self):
        tickers = {"0": {"cik_str": 50863, "ticker": "INTC"}, "1": {"cik_str": 1, "ticker": "NVDA"}}
        ciks, problems = arch.resolve_ciks(tickers, ["INTC", "NVDA", "PYPL"])
        self.assertEqual(ciks, {"INTC": 50863, "NVDA": 1})
        self.assertEqual({p["problem"] for p in problems}, {"cik_mismatch_vs_pinned", "unresolved_or_ambiguous"})

    TXT = (b"<SEC-DOCUMENT>0000050863-26-000157.txt : 20260724\n<SEC-HEADER>x\n"
           b"<ACCEPTANCE-DATETIME>20260723184423\nACCESSION NUMBER:\t\t0000050863-26-000157\n"
           b"CONFORMED SUBMISSION TYPE:\t10-Q\nPUBLIC DOCUMENT COUNT:\t\t2\n"
           b"CONFORMED PERIOD OF REPORT:\t20260627\nFILED AS OF DATE:\t\t20260724\n</SEC-HEADER>\n"
           b"<DOCUMENT>\n<TYPE>10-Q\n<SEQUENCE>1\n<FILENAME>intc-20260627.htm\n<DESCRIPTION>10-Q\n<TEXT>..."
           b"</DOCUMENT>\n<DOCUMENT>\n<TYPE>EX-31.1\n<SEQUENCE>2\n<FILENAME>ex311.htm\n<TEXT>..</DOCUMENT>\n</SEC-DOCUMENT>\n")

    def test_missing_attachment_and_truncated_submission_are_not_complete(self):
        row = {"accessionNumber": "0000050863-26-000157", "form": "10-Q",
               "acceptanceDateTime": "2026-07-23T22:44:23.000Z"}
        missing = self.TXT.replace(b"PUBLIC DOCUMENT COUNT:\t\t2", b"PUBLIC DOCUMENT COUNT:\t\t98")
        self.assertIn("public_document_count_mismatch", arch.check_filing(row, arch.parse_submission_header(missing)))
        truncated = self.TXT[:self.TXT.index(b"<DOCUMENT>\n<TYPE>EX-")]
        issues = arch.check_filing(row, arch.parse_submission_header(truncated))
        self.assertIn("public_document_count_mismatch", issues)
        self.assertIn("submission_truncated", issues)

    def test_header_accepted_at_is_eastern_and_consistent(self):
        header = arch.parse_submission_header(self.TXT)
        self.assertEqual(header["accepted_at"], "2026-07-23T18:44:23-04:00")
        self.assertEqual([d["type"] for d in header["documents"]], ["10-Q", "EX-31.1"])
        row = {"accessionNumber": "0000050863-26-000157", "form": "10-Q"}
        # Either reading of the submissions 'Z' timestamp is accepted, but must match the header.
        for value in ("2026-07-23T18:44:23.000Z", "2026-07-23T22:44:23.000Z"):
            self.assertEqual(arch.check_filing(dict(row, acceptanceDateTime=value), header), [])
        self.assertEqual(arch.check_filing(row, header), [])
        self.assertEqual(arch.check_filing(dict(row, acceptanceDateTime="2026-07-24T09:00:00.000Z"), header),
                         ["acceptance_datetime_disagrees"])

    def test_missing_acceptance_is_never_pit_ready(self):
        txt = self.TXT.replace(b"<ACCEPTANCE-DATETIME>20260723184423\n", b"")
        header = arch.parse_submission_header(txt)
        self.assertNotIn("accepted_at", header)
        problems = arch.check_filing({"accessionNumber": "0000050863-26-000157", "form": "10-Q",
                                      "acceptanceDateTime": ""}, header)
        self.assertIn("acceptance_datetime_missing_in_header", problems)


class MarketTests(unittest.TestCase):
    # Shape of the live Yahoo v8 response for NVDA around the 2024-06-10 10:1 split.
    CHART = {"chart": {"error": None, "result": [{
        "meta": {"exchangeTimezoneName": "America/New_York"},
        "timestamp": [1717767000, 1718026200],
        "events": {"splits": {"1718026200": {"date": 1718026200, "numerator": 10.0, "denominator": 1.0,
                                             "splitRatio": "10:1"}},
                   "dividends": {"1717767000": {"amount": 0.004, "date": 1717767000}}},
        "indicators": {"quote": [{"open": [119.77, 120.37], "high": [121.69, 122.0], "low": [119.0, 117.0],
                                  "close": [120.888, 121.79], "volume": [412386000, 313434100]}]}}]}}

    def test_reconstructs_raw_prices_volume_and_dividend(self):
        chart = arch.parse_yahoo_chart(json.dumps(self.CHART).encode())
        self.assertEqual(chart["splits"][0]["session"], "2024-06-10")
        raw = arch.reconstruct_raw(chart)
        pre, post = raw["bars"]
        self.assertEqual((pre["session"], pre["raw_close"], pre["raw_volume"]), ("2024-06-07", 1208.88, 41238600))
        self.assertEqual((post["raw_close"], post["raw_volume"]), (121.79, 313434100))
        self.assertAlmostEqual(raw["dividends"][0]["amount_reconstructed_raw"], 0.04)
        self.assertEqual(raw["issues"], [])

    def test_non_integer_raw_volume_is_flagged(self):
        data = json.loads(json.dumps(self.CHART))
        data["chart"]["result"][0]["indicators"]["quote"][0]["volume"][0] = 412386003
        raw = arch.reconstruct_raw(arch.parse_yahoo_chart(json.dumps(data).encode()))
        self.assertEqual(raw["issues"][0]["issue"], "non_integer_raw_volume")
        self.assertAlmostEqual(raw["bars"][0]["raw_volume"], 41238600.3)
        self.assertFalse(raw["normalization_verified"])

    def test_identity_mismatch_cannot_download_another_issuer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickers = json.dumps({"0": {"cik_str": 1, "ticker": "NVDA"}}).encode()
            f, calls, _ = make_fetcher(tmp, [(200, tickers, {})])
            result = arch.cmd_sec_index(f, ["NVDA"])
            self.assertEqual(result["symbols"]["NVDA"]["status"], "identity_mismatch_blocked")
            self.assertEqual(len(calls), 1)
            self.assertEqual(list((Path(tmp) / "sec").glob("*/filings-index.json")), [])


class QueueTests(unittest.TestCase):
    def test_limit_resume_and_403_reported_in_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"accessionNumber": f"0000050863-24-00000{i}", "form": form, "filingDate": f"2024-01-0{i}",
                     "acceptanceDateTime": f"2024-01-0{i}T16:01:02.000Z", "size": 10}
                    for i, form in ((1, "10-K"), (2, "8-K"), (3, "4"), (4, "10-K405"))]
            idx = Path(tmp) / "sec" / "0000050863"
            idx.mkdir(parents=True)
            (idx / "filings-index.json").write_text(json.dumps(
                {"symbol": "INTC", "cik": "0000050863", "rows": rows, "findings": []}))
            ok_txt = SecTests.TXT.replace(b"20260723184423", b"20240101160102").replace(
                b"0000050863-26-000157", b"0000050863-24-000001").replace(b"TYPE:\t10-Q", b"TYPE:\t10-K")
            f, calls, _ = make_fetcher(tmp, [(200, ok_txt, {}), (403, b"blocked", {})], max_consecutive_403=5)
            plan = arch.cmd_plan(f.store)
            self.assertEqual(plan["items"], 2)
            self.assertEqual(plan["per_symbol"]["INTC"]["out_of_plan_form_variants"], {"10-K405": 1})
            self.assertEqual(arch.cmd_fetch(f, limit=1)["fetched"], 1)
            second = arch.cmd_fetch(f, limit=5)
            self.assertEqual((second["skipped_cached"], second["failed"]), (1, 1))
            self.assertEqual(len(calls), 2)
            tasks = [{"task_id": "t", "symbol": "INTC", "as_of": "2024-07-11T16:00:00-04:00",
                      "horizon_sessions": 60, "data_plan": None}]
            report = arch.cmd_coverage(f.store, tasks)
            sec = report["symbols"]["INTC"]["sec"]
            self.assertEqual((sec["fetched"], sec["accepted_at_verified"], sec["complete"]), (1, 1, False))
            self.assertEqual(sec["failed"][0]["status"], 403)
            self.assertFalse(report["tasks"][0]["pit_ready"])
            self.assertIn("task_has_no_data_plan", report["tasks"][0]["gaps"])


class TargetsTests(unittest.TestCase):
    def test_suite_has_twenty_fixed_tasks_over_eight_symbols(self):
        tasks = arch.load_targets(ROOT / "benchmarks/suites/nasdaq-diagnostic-v1.toml")
        self.assertEqual(len(tasks), 20)
        self.assertEqual({t["symbol"] for t in tasks},
                         {"AAPL", "INTC", "MSFT", "AMZN", "META", "MU", "PYPL", "NVDA"})


if __name__ == "__main__":
    unittest.main()
