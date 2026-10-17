import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from quant_rsi.adapters.local_parquet import MIRROR_BASE, verify_transfer
from quant_rsi.adapters import hf_edgar as hf


class LocalTransferTests(unittest.TestCase):
    def test_integrity_and_origin_checked_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "local.parquet"
            path.write_bytes(b"a complete source fixture")
            relative = "10-K/example-1.parquet"
            receipt = {"schema": "public-parquet-transfer.v1", "dataset": hf.REPO,
                       "revision": hf.REVISION, "path": relative,
                       "transport_url": MIRROR_BASE + relative,
                       "hash_basis": "public_mirror_lfs_pointer_not_independently_verified_upstream",
                       "size_bytes": path.stat().st_size,
                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            _, verified = verify_transfer(path, json.dumps(receipt).encode(), relative)
            self.assertTrue(verified["full_local_file_hash_verified"])
            self.assertFalse(verified["independently_verified_upstream"])
            for field, value in (("revision", "0" * 40), ("path", "10-K/other.parquet"),
                                 ("transport_url", "https://example.com/data"),
                                 ("sha256", "f" * 64), ("size_bytes", True)):
                with self.subTest(field=field), self.assertRaises(hf.HfEdgarError):
                    verify_transfer(path, json.dumps({**receipt, field: value}).encode(), relative)
            path.write_bytes(b"tampered source fixture!!")
            with self.assertRaises(hf.HfEdgarError):
                verify_transfer(path, json.dumps(receipt).encode(), relative)
