import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant_rsi.adapters import q4_filings as q4  # noqa: E402

META_CDN = "https://d18rn0p25nwr6d.cloudfront.net/CIK-0001326801/"
UUID = "fadc8e6b-6127-46dc-af0e-8276faadd5f6"


def row(fid, form="10-K", date="02/02/2023 00:00:00", symbol="0001326801", docs=None, link=None):
    return {"DocumentList": docs if docs is not None else [
                {"CreateDate": "/Date(1675336109313-0600)/", "DocumentType": "ORIG", "FilingDocumentId": 1, "Url": ""},
                {"CreateDate": "x", "DocumentType": "CONVPDF", "FilingDocumentId": 2, "Url": META_CDN + UUID + ".pdf"},
                {"CreateDate": "x", "DocumentType": "XBRL", "FilingDocumentId": 3, "Url": META_CDN + UUID + ".zip"}],
            "EdgarFilingId": 9086341, "FilingAgentId": 1, "FilingAgentName": "META", "FilingDate": date,
            "FilingDescription": "Annual Report", "FilingId": fid, "FilingTypeId": 7, "FilingTypeMnemonic": form,
            "LinkToDetailPage": link or f"/financials/sec-filings-details/default.aspx?FilingId={fid}",
            "ReceivedDate": "02/02/2023 05:05:23", "ReportPersonId": None, "ReportPersonName": None,
            "StockExchange": "CIK", "StockSymbol": symbol}


def feed(*rows):
    return {"GetEdgarFilingListResult": list(rows)}


def plan(*rows, through="2024-10-04"):
    return q4.plan_feed(feed(*rows), "META", source_sha256="a" * 64, through=through)


class PlanTests(unittest.TestCase):
    def test_identity_mismatch_excluded_with_location(self):
        p = plan(row(1), row(2, symbol="0001364742"), row(3, symbol="0001018724"))
        self.assertEqual([r["raw"]["FilingId"] for r in p["rows"]], [1])
        self.assertEqual([(e["row_index"], e["reason"]) for e in p["excluded"]],
                         [(1, "identity_mismatch"), (2, "identity_mismatch")])
        self.assertFalse(p["ready"])

    def test_row_keeps_raw_fields_hash_and_position(self):
        r0 = row(16357264)
        p = plan(row(5, form="4"), r0)
        got = p["rows"][0]
        self.assertEqual((got["row_index"], got["source_sha256"]), (1, "a" * 64))
        self.assertEqual(got["raw"], r0)
        self.assertIsNone(got["available_at"])
        self.assertEqual(got["detail_url"],
                         "https://investor.atmeta.com/financials/sec-filings-details/default.aspx?FilingId=16357264")

    def test_form_and_date_filter(self):
        p = plan(row(1, form="4"), row(2, form="10-Q/A"), row(3, date="10/04/2024 00:00:00"),
                 row(4, date="10/05/2024 00:00:00"), row(5, form="10-K405"))
        self.assertEqual([r["raw"]["FilingId"] for r in p["rows"]], [2, 3])
        self.assertEqual(p["stats"]["excluded_after_through"], 1)
        self.assertEqual(p["stats"]["excluded_form"], 2)

    def test_date_format_strict(self):
        self.assertEqual(q4.parse_filing_date("02/02/2023 00:00:00"), "2023-02-02")
        for bad in ("2023-02-02", "02/02/2023 05:05:23", "13/02/2023 00:00:00", "02/29/2023 00:00:00", "02/31/2024 00:00:00", None):
            with self.assertRaises(q4.FeedError):
                q4.parse_filing_date(bad)
        p = plan(row(1, date="02/02/2023 05:05:23"))
        self.assertEqual(p["excluded"][0]["reason"], "bad_filing_date")

    def test_documents_roles_and_empty_urls_kept(self):
        docs = plan(row(1))["rows"][0]["documents"]
        orig, pdf, zipdoc = docs
        self.assertEqual((orig["url_issue"], orig["sec_original_bytes_available"]), ("empty_url", False))
        self.assertEqual(pdf["role"], "derived_pdf_q4_conversion")
        self.assertFalse(pdf["sec_original_bytes_available"])
        self.assertEqual(zipdoc["role"], "xbrl_zip_not_full_attachment_set")
        self.assertTrue(zipdoc["fetchable"])
        unknown = plan(row(1, docs=[{"DocumentType": "NEW", "Url": META_CDN + UUID + ".zip"}]))
        self.assertEqual(unknown["rows"][0]["documents"][0]["role"], "unknown_type")
        self.assertFalse(unknown["rows"][0]["documents"][0]["fetchable"])


class UrlTests(unittest.TestCase):
    def test_cdn_url_rules(self):
        ok = META_CDN + UUID + ".zip"
        self.assertEqual(q4.check_cdn_url(ok, "META", ("zip",)), (True, None))
        cases = {
            ok.replace("https", "http", 1): "http_not_upgraded",
            ok.replace("d18rn0p25nwr6d", "unknown-cdn"): "foreign_host",
            ok.replace("https://", "https://u:p@"): "credentials_in_url",
            ok + "?x=1": "query_or_fragment",
            META_CDN + "../CIK-0001018724/" + UUID + ".zip": "path_shape",
            "https://d18rn0p25nwr6d.cloudfront.net/CIK-0001018724/" + UUID + ".zip": "cik_mismatch",
            META_CDN + UUID + ".exe": "extension_not_allowed_for_type",
            "": "empty_url",
            "ftp://d18rn0p25nwr6d.cloudfront.net/x": "scheme",
        }
        for url, reason in cases.items():
            self.assertEqual(q4.check_cdn_url(url, "META", ("zip",)), (False, reason), url)
        self.assertEqual(q4.check_cdn_url(META_CDN + UUID + ".pdf", "META", ("zip",))[1],
                         "extension_not_allowed_for_type")
        legacy = ok.replace(q4.CDN_HOST, q4.LEGACY_META_CDN_HOST)
        self.assertEqual(q4.check_cdn_url(legacy, "META", ("zip",)), (True, None))
        self.assertEqual(q4.check_cdn_url(legacy.replace("0001326801", "0001018724"),
                                        "AMZN", ("zip",)), (False, "foreign_host"))
        self.assertEqual(q4.check_cdn_url(legacy.replace("0001326801", "0001018724"),
                                        "META", ("zip",)), (False, "cik_mismatch"))

    def test_detail_url_rules(self):
        self.assertIsNotNone(q4.detail_url("/financials/sec-filings-details/default.aspx?FilingId=7", "META", 7)[0])
        bad = ["https://evil.example/financials/sec-filings-details/default.aspx?FilingId=7",
               "//evil.example/financials/sec-filings-details/default.aspx?FilingId=7",
               "/financials/sec-filings-details/default.aspx?FilingId=8",
               "/financials/sec-filings-details/default.aspx?FilingId=7&x=1",
               "/sec-filings/sec-filings-details/default.aspx?FilingId=7", ""]
        for link in bad:
            self.assertIsNone(q4.detail_url(link, "META", 7)[0], link)


def ref(acc, form="10-K", date="2023-02-02", cik=1326801, report="2022-12-31"):
    return {"accessionNumber": acc, "form": form, "filingDate": date, "cik": cik, "reportDate": report}


class CrossCheckTests(unittest.TestCase):
    def test_unique_candidate_is_not_verified(self):
        res = q4.cross_check(plan(row(1)), [ref("0001326801-23-000013")])
        m = res["matches"][0]
        self.assertEqual((m["status"], m["accession_candidate"], m["accession_verified"]),
                         ("unique_candidate", "0001326801-23-000013", False))
        self.assertIn("filingDate", m["match_basis"])

    def test_same_day_ambiguity_and_gaps(self):
        p = plan(row(1, form="8-K"), row(2, form="8-K"), row(3, form="10-Q", date="05/01/2023 00:00:00"))
        refs = [ref("0001326801-23-000001", "8-K"), ref("0001326801-23-000002", "8-K"),
                ref("0001326801-23-000003", "10-K/A", "2023-03-01")]
        res = q4.cross_check(p, refs)
        by = {(m["filing_id"], m["status"]) for m in res["matches"]}
        self.assertIn((1, "ambiguous_same_day"), by)
        self.assertIn((2, "ambiguous_same_day"), by)
        self.assertIn((3, "q4_only"), by)
        self.assertIn((None, "reference_only"), by)
        self.assertTrue(all(m["accession_candidate"] is None for m in res["matches"]))

    def test_filing_id_never_used_as_accession(self):
        res = q4.cross_check(plan(row(16357264)), [])
        self.assertIsNone(res["matches"][0]["accession_candidate"])

    def test_reference_rows_filter(self):
        refs = [ref("a"), ref("b", cik=1018724), ref("c", form="4"), ref("d", date="2024-10-05")]
        self.assertEqual([r["accessionNumber"] for r in q4.reference_rows(refs, 1326801, through="2024-10-04")],
                         ["a"])

    def test_detail_page_binding(self):
        p = plan(row(1))["rows"][0]
        refs = [ref("0001326801-23-000013")]
        none = q4.detail_sec_links(b"<html>no links</html>", "META", p, refs)
        self.assertFalse(none["accession_bound"])
        good = q4.detail_sec_links(
            b'<a href="https://www.sec.gov/Archives/edgar/data/1326801/000132680123000013/x.htm">', "META", p, refs)
        self.assertEqual((good["accession"], good["accession_bound"]), ("0001326801-23-000013", True))
        wrong_cik = q4.detail_sec_links(
            b'<a href="https://www.sec.gov/Archives/edgar/data/1018724/000132680123000013/x.htm">', "META", p, refs)
        self.assertFalse(wrong_cik["accession_bound"])
        wrong_form = q4.detail_sec_links(
            b'<a href="https://www.sec.gov/Archives/edgar/data/1326801/000132680123000013/">', "META", p,
            [ref("0001326801-23-000013", form="8-K")])
        self.assertFalse(wrong_form["accession_bound"])
        malformed = q4.detail_sec_links(
            b'<a href="https://www.sec.gov/Archives/edgar/data/1326801/0001326801230000139/x.htm">',
            "META", p, refs)
        self.assertFalse(malformed["accession_bound"])


class CorroborationTests(unittest.TestCase):
    def test_legacy_dei_and_traditional_member_name(self):
        import tempfile
        from scripts.q4_sources import corroborate
        accession = "0001193125-09-154174"
        values = {"EntityCentralIndexKey": "0001018724", "DocumentType": "10-Q",
                  "DocumentPeriodEndDate": "2009-06-30"}
        result = {"documents": [{"member": "amzn-20090630.xml", "facts": [
            {"concept": {"namespace": "http://xbrl.us/dei/2008-03-31", "local_name": k},
             "value": v, "status": "ok"} for k, v in values.items()]}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "AMZN.jsonl"
            p.write_text(json.dumps(ref(accession, "10-Q", "2009-07-24", 1018724, "2009-06-30")))
            check = corroborate(result, "AMZN", accession, tmp)
            for fact in result["documents"][0]["facts"]:
                fact["concept"]["namespace"] = "http://xbrl.us/dei/2009-01-31"
            legacy_2009 = corroborate(result, "AMZN", accession, tmp)
            self.assertEqual(legacy_2009["checks"], check["checks"])
        self.assertTrue(all(check["checks"][k] for k in ("cik", "form", "period")))
        self.assertIsNone(check["checks"]["member_stem_is_primary_document"])
        self.assertFalse(check["accession_verified"])

    def test_amended_form_must_match_exactly(self):
        import tempfile
        from scripts.q4_sources import corroborate
        accession = "0001326801-22-000028"
        fact = {"concept": {"namespace": "http://xbrl.sec.gov/dei/2021q4", "local_name": "DocumentType"},
                "value": "8-K/A", "status": "ok"}
        result = {"documents": [{"member": "fb-20220110_htm.xml", "facts": [fact]}]}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "META.jsonl"
            p.write_text(json.dumps(dict(ref(accession, "8-K/A"), primaryDocument="fb-20220110.htm")))
            self.assertTrue(corroborate(result, "META", accession, tmp)["checks"]["form"])
            fact["value"] = "8-K"
            result["documents"][0]["facts"].append({"concept": {
                "namespace": "http://xbrl.sec.gov/dei/2021q4", "local_name": "AmendmentFlag"},
                "value": "true", "status": "ok"})
            check = corroborate(result, "META", accession, tmp)
            self.assertFalse(check["checks"]["form"])
            self.assertTrue(check["base_form_with_amendment_flag_agrees"])
            self.assertTrue(check["requires_context_review"])
            self.assertNotIn("content agrees", check["note"])


class FetcherTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from scripts.archive_sources import Store
        from scripts import q4_sources
        self.mod = q4_sources
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.meta = {"symbol": "META", "filing_id": 1, "document_type": "XBRL"}

    def _fetcher(self, responses, **kw):
        calls = []

        class Resp:
            def __init__(self, status, body):
                self.status, self.headers, self._body = status, {}, body

            def read(self, n=-1):
                return self._body[:n] if n >= 0 else self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            calls.append((req.full_url, timeout))
            status, body = responses.pop(0)
            if status != 200:
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, None)
            return Resp(status, body)
        f = self.mod.SampleFetcher(self.store, opener=opener, sleep=lambda s: None, **kw)
        return f, calls

    def test_remainder_cap_and_cache_identity(self):
        f, calls = self._fetcher([(200, b"abcdef")], max_bytes=3)
        rec = f.get("limited", META_CDN + UUID + ".zip", self.meta)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error_detail"], "over_remaining_byte_budget")
        self.assertLessEqual(f.bytes, 4)  # remaining budget plus one overflow sentinel
        self.assertNotIn("limited", self.store.catalog())
        with self.assertRaises(self.mod.BudgetExceeded):
            f.get("next", META_CDN + UUID + ".zip", self.meta)
        self.assertEqual(len(calls), 1)
        g, _ = self._fetcher([(200, b"ok")])
        g.get("cached", META_CDN + UUID + ".zip", self.meta)
        with self.assertRaises(q4.FeedError):
            g.get("cached", META_CDN + UUID + ".zip", dict(self.meta, filing_id=2))

    def test_refuses_foreign_url_before_network_or_cache(self):
        f, calls = self._fetcher([])
        with self.assertRaises(q4.FeedError):
            f.get("bad", "https://example.invalid/a", self.meta)
        self.assertEqual(calls, [])

    def test_incomplete_response_never_enters_success_catalog(self):
        import http.client
        def broken(req, timeout):
            raise http.client.IncompleteRead(b"partial", 100)
        f = self.mod.SampleFetcher(self.store, opener=broken)
        rec = f.get("partial", META_CDN + UUID + ".zip", self.meta)
        self.assertFalse(rec["ok"])
        self.assertIn("IncompleteRead", rec["error"])
        self.assertNotIn("partial", self.store.catalog())

    def test_oversize_is_failure_not_success(self):
        old = self.mod.MAX_FILE
        self.mod.MAX_FILE = 10
        try:
            f, _ = self._fetcher([(200, b"x" * 11)])
            rec = f.get("k", META_CDN + UUID + ".zip", self.meta)
        finally:
            self.mod.MAX_FILE = old
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["error_detail"], "over_64MiB_cap_not_complete")
        self.assertNotIn("k", self.store.catalog())

    def test_429_stops_and_budget_enforced(self):
        f, calls = self._fetcher([(429, b""), (200, b"ok")])
        self.assertFalse(f.get("a", META_CDN + UUID + ".zip", self.meta)["ok"])
        with self.assertRaises(self.mod.BudgetExceeded):
            f.get("b", META_CDN + UUID + ".zip", self.meta)
        self.assertEqual(len(calls), 1)
        g, calls = self._fetcher([(200, b"1"), (200, b"2")], max_requests=1)
        self.assertTrue(g.get("c", META_CDN + UUID + ".zip", self.meta)["ok"])
        self.assertTrue(g.get("c", META_CDN + UUID + ".zip", self.meta)["cache_hit"])
        with self.assertRaises(self.mod.BudgetExceeded):
            g.get("d", META_CDN + UUID + ".zip", self.meta)
        self.assertEqual(calls[0][1], 45.0)


SOURCE = ROOT / "artifacts/benchmark-quality/issuer-recheck-20261017"


@unittest.skipUnless((SOURCE / "catalog.jsonl").exists() and not os.environ.get("Q4_SKIP_CACHED"),
                     "cached Q4 feed not present")
class CachedFeedTests(unittest.TestCase):
    def test_real_feed_plans_and_crosschecks(self):
        from scripts.archive_sources import Store
        store = Store(SOURCE)
        refdir = ROOT / "artifacts/benchmark-quality/public-index/import-2026-09-30-v2"
        for symbol, key, rows in (("META", "meta-financials-all-sec-feed", 4156),
                                  ("AMZN", "amzn-sec-filings-all-sec-feed", 3062)):
            rec = store.catalog()[key]
            data = json.loads(store.read(rec["sha256"]))
            p = q4.plan_feed(data, symbol, source_sha256=rec["sha256"], through="2024-10-04")
            self.assertEqual(p["stats"]["feed_rows"], rows)
            self.assertGreater(p["stats"]["excluded_identity_mismatch"], 0)
            self.assertTrue(all(r["raw"]["StockSymbol"] == q4.cik10(p["cik"]) for r in p["rows"]))
            refs = [json.loads(line) for line in (refdir / f"{symbol}.jsonl").read_text().splitlines() if line.strip()]
            res = q4.cross_check(p, q4.reference_rows(refs, p["cik"], through="2024-10-04"))
            self.assertFalse(any(m["accession_verified"] for m in res["matches"]))
            self.assertIn("ambiguous_same_day", res["stats"])


if __name__ == "__main__":
    unittest.main()
