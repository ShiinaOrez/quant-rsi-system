"""Read one complete, hash-checked local copy of the pinned public corpus.

Transport provenance stays separate from the dataset's claimed origin. A mirror
checksum establishes transfer integrity, not independent verification by SEC or HF.
No network extension, token, alternate host, or automatic fallback is used here.
"""
import hashlib
import json
from pathlib import Path

from quant_rsi.adapters import hf_edgar as hf

MIRROR_BASE = f"https://hf-mirror.com/datasets/{hf.REPO}/resolve/{hf.REVISION}/"


def verify_transfer(path, receipt_data, expected_path):
    hf.safe_path(expected_path)
    receipt = json.loads(receipt_data)
    if (receipt.get("schema") != "public-parquet-transfer.v1" or
            receipt.get("dataset") != hf.REPO or receipt.get("revision") != hf.REVISION or
            receipt.get("path") != expected_path or
            receipt.get("transport_url") != MIRROR_BASE + expected_path):
        raise hf.HfEdgarError("local_transfer_identity_mismatch")
    if receipt.get("hash_basis") != "public_mirror_lfs_pointer_not_independently_verified_upstream":
        raise hf.HfEdgarError("local_transfer_hash_basis_required")
    size, expected = receipt.get("size_bytes"), receipt.get("sha256")
    if type(size) is not int or size <= 0 or not isinstance(expected, str):
        raise hf.HfEdgarError("local_transfer_size_or_hash_invalid")
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise hf.HfEdgarError("local_transfer_hash_invalid")
    path = Path(path).resolve(strict=True)
    if path.stat().st_size != size:
        raise hf.HfEdgarError("local_transfer_size_mismatch")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise hf.HfEdgarError("local_transfer_checksum_mismatch")
    return path, dict(receipt, receipt_sha256=hashlib.sha256(receipt_data).hexdigest(),
                      full_local_file_hash_verified=True, independently_verified_upstream=False)


class LocalParquetReader(hf.DuckDBReader):
    """Use the same parameterised queries on one validated local file, without HTTP."""

    def __init__(self, path, receipt_path, expected_path):
        self.path, self.transport_evidence = verify_transfer(
            path, Path(receipt_path).read_bytes(), expected_path)
        self.origin_url = hf.BASE_URL + expected_path
        import duckdb
        self.connection = duckdb.connect(":memory:")
        self.connection.execute("SET autoinstall_known_extensions=false; "
                                "SET autoload_known_extensions=false; SET threads=1; "
                                "SET memory_limit='2GB'; SET lock_configuration=true")

    def _check(self, url):
        if url != self.origin_url:
            raise hf.HfEdgarError("local_reader_source_mismatch")
        return str(self.path)
