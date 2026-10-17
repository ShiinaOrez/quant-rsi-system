import io
import tempfile
import unittest
import zipfile

from scripts.archive_sources import Store
from scripts.inventory_mirror_xbrl import inventory


class InventoryTest(unittest.TestCase):
    def test_valid_cached_instance_and_missing_blob_are_both_reported(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            queue_path = store.root / "mirror" / "INTC" / "queue.jsonl"
            queue_path.parent.mkdir(parents=True)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("report.xml", b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>')
            for index in range(2):
                key = f"filing-{index}"
                store.append(queue_path, {"key": key, "accession": f"0000050863-24-00000{index}",
                                          "form": "10-Q", "filing_date": "2024-01-01",
                                          "role": "xbrl_zip", "url": "https://example.org/filing.zip"})
                if index == 0:
                    store.append(store.catalog_path, {"key": key, "sha256": store.put(buffer.getvalue())})
            result = inventory(root, "INTC")
            self.assertEqual((result["zip_count"], result["instance_count"]), (2, 1))
            self.assertEqual(len(result["problems"]), 1)
            self.assertEqual(result["filings"][1]["classification_error"], "cached_blob_missing_or_corrupt")
            self.assertFalse(result["benchmark_ready"])


if __name__ == "__main__":
    unittest.main()
