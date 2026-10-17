import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from scripts.import_submissions_reference import assess_timestamp, checked_row, validate_release


class ReferenceTests(unittest.TestCase):
    def test_upstream_support_does_not_mean_independent_verification(self):
        row = {"cik": 320193, "accessionNumber": "0001193125-23-000001",
               "filingDate": datetime(2023, 8, 4).date(),
               "acceptanceDateTime": datetime(2023, 8, 3, 22, 4, 43, tzinfo=timezone.utc),
               "timestamp_provenance": "trusted_snapshot_eastern"}
        result = checked_row(row, 320193)
        self.assertFalse(result["independently_verified_publication_time"])
        self.assertEqual(result["timestamp_evidence_status"], "upstream_supported_not_independently_verified")
        # Submitter's accession prefix may legitimately differ from the issuer CIK.
        self.assertEqual(result["accessionNumber"], row["accessionNumber"])
        with self.assertRaisesRegex(ValueError, "CIK"):
            checked_row(row, 50863)

    def test_fallback_unknown_missing_and_naive_fail_closed(self):
        value = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(assess_timestamp({"acceptanceDateTime": value,
                                          "timestamp_provenance": "unresolved_raw_as_eastern"}), "unresolved")
        self.assertEqual(assess_timestamp({"acceptanceDateTime": value,
                                          "timestamp_provenance": "new_rule"}), "unknown_provenance")
        self.assertEqual(assess_timestamp({"acceptanceDateTime": None,
                                          "timestamp_provenance": "sgml"}), "missing")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            assess_timestamp({"acceptanceDateTime": datetime(2023, 1, 1), "timestamp_provenance": "sgml"})

    def test_manifest_and_payload_both_pinned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload, manifest = root / "filings.parquet", root / "manifest.json"
            payload.write_bytes(b"example parquet bytes")
            asset = {"name": "filings.parquet", "size_bytes": payload.stat().st_size,
                     "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}
            manifest.write_text(json.dumps({"version": "2026-09-30", "license": "CC-BY-4.0",
                                            "population": {"rows": 27}, "files": [asset]}))
            source = {"manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                      "version": "2026-09-30", "license": "CC-BY-4.0", "population_rows": 27,
                      "filings_sha256": asset["sha256"], "filings_size_bytes": asset["size_bytes"]}
            self.assertEqual(validate_release(manifest, payload, source)["version"], "2026-09-30")
            payload.write_bytes(b"x" * asset["size_bytes"])
            with self.assertRaisesRegex(ValueError, "filings checksum"):
                validate_release(manifest, payload, source)
            payload.write_bytes(b"example parquet bytes")
            manifest.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "manifest checksum"):
                validate_release(manifest, payload, source)


if __name__ == "__main__":
    unittest.main()
