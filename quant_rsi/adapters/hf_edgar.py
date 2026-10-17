"""Controlled adapter for the public Hugging Face dataset TeraflopAI/SEC-EDGAR.

The dataset is a secondary corpus built by third parties (datamule). Per row, ``content`` is the
dataset's main-document field and ``text`` its extracted text. They are not independently
verified against SEC original bytes; a row does not establish all attachments. This module only:

* validates a saved Hub API manifest against the pinned repo/revision and builds resolve URLs
  from safe relative paths listed in that manifest (no other host, revision or path);
* checks a metadata row (accession, filer CIK, form, filing date) against one fixed reference
  row from the versioned submissions index; empty, duplicate, conflicting or wrong-identity
  rows raise HfEdgarError. Raw field strings are kept next to their parsed structure;
* rejects Dataset Viewer previews (``truncated_cells`` or any non-pinned source);
* plans a content read from Parquet footer metadata: DuckDB reads whole column chunks, so the
  compressed size of every requested column in the containing row group is checked against an
  explicit limit. This is a preflight, not a hard cap on network traffic;
* writes content exactly as the field's UTF-8 string with SHA-256, never overwriting bytes.

No accepted/publication time is bound, nothing is marked ready, and output is build material,
not agent-visible history. The DuckDB reader is optional and imported lazily; it loads the
already installed httpfs extension, never installs one, and creates no secrets/tokens.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "hf-edgar-source.v1"
REPO = "TeraflopAI/SEC-EDGAR"
REVISION = "43de32c4c3cd4a56c26757345a4ec6db04fccda0"
BASE_URL = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/"
ROLE = ("secondary_dataset_main_document_field; SEC original bytes not independently verified; attachments not "
        "included or claimed")
METADATA_COLUMNS = ("metadata_accession-number", "metadata_filing-date", "metadata_documents",
                    "metadata_period", "metadata_filer")
CONTENT_COLUMNS = ("content",) + METADATA_COLUMNS
DEFAULT_MAX_ROW_GROUP_BYTES = 100_000_000

_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}/[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.parquet")
_FORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9/-]{0,31}")
_DATE8 = re.compile(r"[0-9]{8}")
_DATE10 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class HfEdgarError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    if (not isinstance(path, str) or not _PATH.fullmatch(path) or ".." in path
            or path.split("/")[1].startswith(".")):
        raise HfEdgarError(f"unsafe_path:{path!r}")
    return path


def load_manifest(data):
    """Validate saved Hub API bytes; return revision, manifest hash and parquet paths."""
    if not isinstance(data, (bytes, bytearray)):
        raise HfEdgarError("manifest_must_be_bytes")
    try:
        manifest = json.loads(data)
    except ValueError as error:
        raise HfEdgarError(f"manifest_not_json:{error}") from None
    if not isinstance(manifest, dict) or manifest.get("id") != REPO:
        raise HfEdgarError("manifest_wrong_repo")
    if manifest.get("sha") != REVISION:
        raise HfEdgarError(f"manifest_revision_mismatch:{manifest.get('sha')!r}")
    if manifest.get("private") is not False or manifest.get("gated") is not False:
        raise HfEdgarError("manifest_not_public")
    siblings = manifest.get("siblings")
    if not isinstance(siblings, list):
        raise HfEdgarError("manifest_missing_siblings")
    paths = []
    for item in siblings:
        name = item.get("rfilename") if isinstance(item, dict) else None
        if isinstance(name, str) and name.endswith(".parquet"):
            paths.append(safe_path(name))
    if len(set(paths)) != len(paths):
        raise HfEdgarError("manifest_duplicate_path")
    if not paths:
        raise HfEdgarError("manifest_no_parquet")
    return {"repo": REPO, "revision": REVISION, "manifest_sha256": sha256(bytes(data)),
            "paths": tuple(paths)}


def shards(manifest, form):
    if not isinstance(form, str) or not _FORM.fullmatch(form):
        raise HfEdgarError(f"bad_form:{form!r}")
    return [path for path in manifest["paths"] if path.split("/")[0] == form]


def resolve_url(manifest, path):
    if manifest.get("revision") != REVISION or path not in manifest["paths"]:
        raise HfEdgarError(f"path_not_in_manifest:{path!r}")
    return BASE_URL + safe_path(path)


def source(manifest, path, file_row_number):
    if type(file_row_number) is not int or file_row_number < 0:
        raise HfEdgarError(f"bad_row_number:{file_row_number!r}")
    return {"dataset": REPO, "revision": REVISION, "path": path,
            "url": resolve_url(manifest, path), "file_row_number": file_row_number,
            "manifest_sha256": manifest["manifest_sha256"]}


def reference_from_rows(rows, accession, cik):
    """Pick exactly one submissions-index row for (accession, cik); copy its identity."""
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise HfEdgarError(f"bad_accession:{accession!r}")
    hits = [row for row in rows if row.get("accessionNumber") == accession
            and row.get("cik") == cik]
    if len(hits) != 1:
        raise HfEdgarError(f"reference_rows:{len(hits)}")
    row = hits[0]
    reference = {"accession": accession, "cik": cik, "form": row.get("form"),
                 "filing_date": row.get("filingDate")}
    if (type(cik) is not int or cik <= 0 or not isinstance(reference["form"], str)
            or not _FORM.fullmatch(reference["form"])
            or not isinstance(reference["filing_date"], str)
            or not _DATE10.fullmatch(reference["filing_date"])):
        raise HfEdgarError(f"bad_reference:{reference!r}")
    return reference


def _filers(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise HfEdgarError("empty:metadata_filer")
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise HfEdgarError(f"filer_not_json:{error}") from None
    items = parsed if isinstance(parsed, list) else [parsed]
    if not items or not all(isinstance(item, dict) for item in items):
        raise HfEdgarError("filer_not_object")
    return parsed, items


def _filer_cik(item):
    value = (item.get("company-data") or {}).get("cik")
    if not isinstance(value, str) or not value.isdigit():
        raise HfEdgarError(f"filer_bad_cik:{value!r}")
    return int(value)


def check_metadata(row, reference):
    """Validate one metadata row against the reference; return raw and parsed fields.

    ``row`` holds raw column strings plus ``file_row_number``; a ``truncated_cells`` key (Dataset
    Viewer) is refused even when empty, because preview rows are not source rows.
    """
    if "truncated_cells" in row:
        raise HfEdgarError("viewer_preview_refused")
    for column in ("metadata_accession-number", "metadata_filing-date", "metadata_filer"):
        if not isinstance(row.get(column), str) or not row[column].strip():
            raise HfEdgarError(f"empty:{column}")
    accession = row["metadata_accession-number"]
    if accession != reference["accession"]:
        raise HfEdgarError(f"accession_mismatch:{accession!r}")
    date = row["metadata_filing-date"]
    if not _DATE8.fullmatch(date) or f"{date[:4]}-{date[4:6]}-{date[6:]}" != reference[
            "filing_date"]:
        raise HfEdgarError(f"filing_date_mismatch:{date!r}")
    parsed, items = _filers(row["metadata_filer"])
    matched = [item for item in items if _filer_cik(item) == reference["cik"]]
    if len(matched) != 1:
        raise HfEdgarError(f"filer_cik_mismatch:{[_filer_cik(i) for i in items]}")
    form = (matched[0].get("filing-values") or {}).get("form-type")
    if form != reference["form"]:
        raise HfEdgarError(f"form_mismatch:{form!r}")
    raw = {column: row.get(column) for column in METADATA_COLUMNS if column in row}
    return {"raw": raw, "parsed": {"filer": parsed, "matched_filer_index": items.index(
        matched[0]), "documents": describe_documents(row.get("metadata_documents"))}}


def describe_documents(raw):
    """Report what metadata_documents actually holds; no meaning is inferred from it."""
    if raw is None:
        return {"present": False}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"present": True, "json": False, "length": len(raw)}
    items = parsed if isinstance(parsed, list) else [parsed]
    keys = sorted({key for item in items if isinstance(item, dict) for key in item})
    return {"present": True, "json": True, "type": type(parsed).__name__, "items": len(items),
            "keys": keys, "parsed": parsed}


def unique_match(rows, reference):
    """Locate exactly one row for the reference accession among scan rows.

    Several rows with the same accession are refused (``duplicate_rows`` when their metadata is
    identical, ``conflicting_rows`` otherwise); the caller must not pick one silently.
    """
    hits = [row for row in rows if row.get("metadata_accession-number") == reference["accession"]]
    if not hits:
        raise HfEdgarError("accession_not_found")
    if len(hits) > 1:
        same = len({json.dumps({c: r.get(c) for c in METADATA_COLUMNS}, sort_keys=True)
                    for r in hits}) == 1
        raise HfEdgarError(("duplicate_rows:" if same else "conflicting_rows:")
                           + ",".join(str(r.get("file_row_number")) for r in hits))
    return hits[0], check_metadata(hits[0], reference)


def plan_row_group(footer, file_row_number, columns, max_bytes):
    """Find the row group holding the row; refuse if its requested chunks exceed max_bytes.

    ``footer`` rows: row_group_id, row_group_num_rows, path_in_schema, total_compressed_size
    (DuckDB parquet_metadata). Sizes are compressed column-chunk bytes, which DuckDB reads in full
    for any selected row in that group; HTTP overhead and footer reads are not counted.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise HfEdgarError("bad_max_bytes")
    if type(file_row_number) is not int or file_row_number < 0:
        raise HfEdgarError("bad_row_number")
    groups = {}
    for row_group_id, num_rows, column, size in footer:
        if (type(row_group_id) is not int or row_group_id < 0 or
                type(num_rows) is not int or num_rows <= 0 or
                type(size) is not int or size < 0):
            raise HfEdgarError("footer_invalid_size_or_group")
        group = groups.setdefault(row_group_id, {"rows": num_rows, "columns": {}})
        if group["rows"] != num_rows or column in group["columns"]:
            raise HfEdgarError(f"footer_inconsistent:{row_group_id}")
        group["columns"][column] = size
    start = 0
    for row_group_id in sorted(groups):
        group = groups[row_group_id]
        if start <= file_row_number < start + group["rows"]:
            missing = [c for c in columns if c not in group["columns"]]
            if missing:
                raise HfEdgarError(f"footer_missing_columns:{missing}")
            sizes = {c: group["columns"][c] for c in columns}
            plan = {"row_group_id": row_group_id, "row_group_first_row": start,
                    "row_group_rows": group["rows"], "compressed_bytes": sizes,
                    "total_compressed_bytes": sum(sizes.values()), "max_bytes": max_bytes,
                    "limit_kind": "preflight_column_chunk_estimate_not_network_cap"}
            if plan["total_compressed_bytes"] > max_bytes:
                raise HfEdgarError(f"row_group_too_large:{plan['total_compressed_bytes']}"
                                   f">{max_bytes}")
            return plan
        start += group["rows"]
    raise HfEdgarError(f"row_out_of_range:{file_row_number}>={start}")


def _write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def export_content(out_dir, origin, reference, row, plan):
    """Write the content field verbatim as UTF-8 plus a provenance record; never overwrite."""
    checked = check_metadata(row, reference)
    if row.get("file_row_number") != origin["file_row_number"]:
        raise HfEdgarError(f"row_number_mismatch:{row.get('file_row_number')!r}")
    content = row.get("content")
    if not isinstance(content, str) or not content:
        raise HfEdgarError("empty:content")
    data = content.encode("utf-8")
    digest = sha256(data)
    stem = f"{reference['accession']}.content"
    target, record = Path(out_dir) / stem, Path(out_dir) / f"{stem}.provenance.json"
    if target.exists() or record.exists():
        raise HfEdgarError(f"output_exists:{target}")
    provenance = {
        "schema": SCHEMA, "role": ROLE, "source": origin, "reference": reference,
        "metadata": checked, "row_group_plan": plan,
        "content": {"field": "content", "file": target.name, "encoding": "utf-8",
                    "bytes": len(data), "sha256": digest,
                    "transform": "none; Parquet UTF8 string re-encoded as UTF-8"},
        "text": "not_read; text is a derived field and never substitutes content",
        "accepted_at": None, "ready": False, "agent_visible": False,
        "exported_at": datetime.now(timezone.utc).isoformat()}
    _write_new(target, data)
    _write_new(record, (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode())
    return provenance


def write_failure(out_dir, name, detail):
    """Keep a failure record under a fresh name; returns its path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path(out_dir) / f"failure-{name}-{stamp}.json"
    _write_new(path, (json.dumps({"schema": SCHEMA, "status": "error", **detail},
                                 indent=2, sort_keys=True, default=str) + "\n").encode())
    return path


def cached_receipt(path, origin_path, targets_sha256, reference_sha256=None, manifest_sha256=None):
    """Return a completed receipt for the same revision/shard/targets, else None."""
    try:
        receipt = json.loads(Path(path).read_bytes())
    except (FileNotFoundError, ValueError):
        return None
    if (receipt.get("schema") == SCHEMA and receipt.get("status") == "ok"
            and receipt.get("revision") == REVISION and receipt.get("path") == origin_path
            and receipt.get("targets_sha256") == targets_sha256
            and (reference_sha256 is None or receipt.get("reference", {}).get("sha256") == reference_sha256)
            and (manifest_sha256 is None or receipt.get("manifest_sha256") == manifest_sha256)):
        return receipt
    return None


class DuckDBReader:
    """Read-only DuckDB/httpfs boundary. Queries are parameterised; the URL comes from
    resolve_url only. timeout/retries apply per HTTP request inside DuckDB, not per command."""

    def __init__(self, timeout=30, retries=1):
        import duckdb
        if not (1 <= int(timeout) <= 120 and 0 <= int(retries) <= 3):
            raise HfEdgarError("timeout_or_retries_out_of_range")
        self.connection = duckdb.connect(":memory:")
        for statement in ("SET autoinstall_known_extensions=false",
                          "SET autoload_known_extensions=false", "LOAD httpfs",
                          f"SET http_timeout={int(timeout)}", f"SET http_retries={int(retries)}",
                          "SET threads=1", "SET enable_progress_bar=false",
                          "SET lock_configuration=true"):
            self.connection.execute(statement)

    def _check(self, url):
        if not url.startswith(BASE_URL) or not _PATH.fullmatch(url[len(BASE_URL):]):
            raise HfEdgarError(f"url_refused:{url!r}")
        return url

    def footer(self, url):
        return self.connection.execute(
            "SELECT row_group_id, row_group_num_rows, path_in_schema, total_compressed_size "
            "FROM parquet_metadata(?)", [self._check(url)]).fetchall()

    def accessions(self, url):
        """Project only the accession column (plus row number) for every row."""
        return self.connection.execute(
            'SELECT "metadata_accession-number", file_row_number '
            "FROM read_parquet(?, file_row_number=true)", [self._check(url)]).fetchall()

    def metadata(self, url, row_numbers):
        if not row_numbers:
            return []
        cols = ", ".join(f'"{c}"' for c in METADATA_COLUMNS)
        cursor = self.connection.execute(
            f"SELECT {cols}, file_row_number FROM read_parquet(?, file_row_number=true) "
            "WHERE list_contains(?, file_row_number) ORDER BY file_row_number",
            [self._check(url), list(row_numbers)])
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, values)) for values in cursor.fetchall()]

    def full_row(self, url, file_row_number, accession):
        cols = ", ".join(f'"{c}"' for c in CONTENT_COLUMNS)
        cursor = self.connection.execute(
            f"SELECT {cols}, file_row_number FROM read_parquet(?, file_row_number=true) "
            'WHERE file_row_number = ? AND "metadata_accession-number" = ?',
            [self._check(url), file_row_number, accession])
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, values)) for values in cursor.fetchall()]
