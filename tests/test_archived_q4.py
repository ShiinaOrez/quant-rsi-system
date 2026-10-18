import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from quant_rsi.adapters.archived_q4 import plan_capture
from quant_rsi.adapters import q4_filings as q4
from tests.test_q4_filings import row, feed, UUID


class ArchivedTransportTests(unittest.TestCase):
    def setUp(self):
        url = f"http://{q4.CDN_HOST}/CIK-0000320193/{UUID}.zip"
        self.raw = json.dumps(feed(row(1, symbol="0000320193", docs=[
            {"DocumentType": "XBRL", "Url": url}],
            link="/sec-filings/sec-filings-details/default.aspx?FilingId=1"))).encode()
        self.policy = {"schema": "archived-q4-https.v1", "issuer": "AAPL", "cik": 320193,
                       "source_origin": "https://investor.apple.com",
                       "source_path": "/feed/SECFiling.svc/GetEdgarFilingList",
                       "capture_timestamp": "20200730205234", "payload_bytes": len(self.raw),
                       "payload_sha256": hashlib.sha256(self.raw).hexdigest(),
                       "cdx_digest_sha1_base32": base64.b32encode(hashlib.sha1(self.raw).digest()).decode(),
                       "transport_operation": "http_to_https_same_host_and_path",
                       "sec_original_bytes_verified": False, "publication_time_verified": False}

    def test_explicit_policy_preserves_observed_url_and_visibility(self):
        strict = q4.plan_feed(json.loads(self.raw), "AAPL", source_sha256=self.policy["payload_sha256"],
                              through="2024-10-04")
        self.assertFalse(strict["rows"][0]["documents"][0]["fetchable"])
        planned = plan_capture(self.raw, self.policy, through="2024-10-04")
        doc = planned["rows"][0]["documents"][0]
        self.assertTrue(doc["fetchable"])
        self.assertTrue(doc["url"].startswith("http://"))
        self.assertEqual(doc["fetch_url"], doc["url"].replace("http://", "https://", 1))
        self.assertFalse(doc["sec_original_bytes_available"])
        self.assertIsNone(planned["rows"][0]["available_at"])
        self.assertFalse(planned["ready"])

    def test_capture_and_scope_mutations_are_refused(self):
        for field, value in (("payload_sha256", "0" * 64), ("payload_bytes", True),
                             ("cdx_digest_sha1_base32", "BAD"), ("cik", 1326801),
                             ("source_origin", "https://evil.invalid"),
                             ("capture_timestamp", "20201330205234"),
                             ("transport_operation", "unrestricted"),
                             ("sec_original_bytes_verified", True),
                             ("publication_time_verified", True)):
            with self.subTest(field=field), self.assertRaises(q4.FeedError):
                plan_capture(self.raw, dict(self.policy, **{field: value}), through="2024-10-04")

    def test_rewrite_cannot_admit_foreign_host_cik_or_query(self):
        for old, new in ((q4.CDN_HOST, "evil.invalid"), ("CIK-0000320193", "CIK-0001018724"),
                         (".zip", ".zip?credential=not-a-real-key")):
            raw = self.raw.replace(old.encode(), new.encode())
            policy = dict(self.policy, payload_bytes=len(raw), payload_sha256=hashlib.sha256(raw).hexdigest(),
                          cdx_digest_sha1_base32=base64.b32encode(hashlib.sha1(raw).digest()).decode())
            doc = plan_capture(raw, policy, through="2024-10-04")["rows"][0]["documents"][0]
            self.assertFalse(doc["fetchable"])
            self.assertIsNone(doc["fetch_url"])

    def test_offline_pipeline_parses_apple_without_promoting_candidate(self):
        from scripts.archived_q4_sources import make_plan
        from scripts.q4_sources import cmd_parse
        from scripts.archive_sources import Store
        from tests.test_xbrl_instance import instance, num
        acc = "0000320193-23-000001"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "capture.json"
            capture.write_bytes(self.raw)
            policy = root / "policy.toml"
            policy.write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in self.policy.items()))
            refs = root / "refs"
            refs.mkdir()
            (refs / "AAPL.jsonl").write_text(json.dumps({"accessionNumber": acc, "cik": 320193,
                "form": "10-K", "filingDate": "2023-02-02", "reportDate": "2012-12-29",
                "primaryDocument": "aapl-20121229.htm"}))
            out = root / "out"
            make_plan(capture, policy, refs, out, "2024-10-04")
            body = num("123") + "".join(f'<dei:{name} contextRef="c-i">{value}</dei:{name}>'
                for name, value in (("EntityCentralIndexKey", "0000320193"),
                                    ("DocumentType", "10-K"), ("DocumentPeriodEndDate", "2012-12-29")))
            xml = instance(body).replace(b"0000050863", b"0000320193").replace(b'<xbrli:xbrl ',
                b'<xbrli:xbrl xmlns:dei="http://xbrl.sec.gov/dei/2023" ', 1)
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, "w") as archive:
                archive.writestr("aapl-20121229_htm.xml", xml)
            raw = memory.getvalue()
            store = Store(root / "store")
            store.append(store.catalog_path, {"key": "q4-xbrl-AAPL-1", "sha256": store.put(raw),
                "bytes": len(raw), "url": f"https://{q4.CDN_HOST}/CIK-0000320193/{UUID}.zip",
                "meta": {"symbol": "AAPL", "filing_id": 1, "document_type": "XBRL"}})
            parsed = cmd_parse(store, out, refs)[0]
            self.assertFalse(parsed["identity"]["accession_verified"])
            self.assertFalse(parsed["ready"])
            self.assertTrue(all(parsed["corroboration"]["checks"].values()))
            self.assertEqual(parsed["parse"]["counts"]["pit_ready_facts"], 0)
            self.assertEqual(parsed["parse"]["counts"]["numericItem"]["by_status"], {"ok": 1})


if __name__ == "__main__":
    unittest.main()
