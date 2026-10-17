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
MAX_GROUP_TARGETS = 20

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


def _footer_groups(footer):
    """Validate DuckDB parquet_metadata rows; return [(row_group_id, first_row, rows, sizes)]."""
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
    result, start = [], 0
    for row_group_id in sorted(groups):
        result.append((row_group_id, start, groups[row_group_id]["rows"],
                       groups[row_group_id]["columns"]))
        start += groups[row_group_id]["rows"]
    return result


def _group_plan(group, columns, max_bytes):
    row_group_id, start, rows, sizes = group
    missing = [c for c in columns if c not in sizes]
    if missing:
        raise HfEdgarError(f"footer_missing_columns:{missing}")
    chosen = {c: sizes[c] for c in columns}
    plan = {"row_group_id": row_group_id, "row_group_first_row": start,
            "row_group_rows": rows, "compressed_bytes": chosen,
            "total_compressed_bytes": sum(chosen.values()), "max_bytes": max_bytes,
            "limit_kind": "preflight_column_chunk_estimate_not_network_cap"}
    if plan["total_compressed_bytes"] > max_bytes:
        raise HfEdgarError(f"row_group_too_large:{row_group_id}:"
                           f"{plan['total_compressed_bytes']}>{max_bytes}")
    return plan


def _find_group(groups, file_row_number):
    if type(file_row_number) is not int or file_row_number < 0:
        raise HfEdgarError("bad_row_number")
    for group in groups:
        if group[1] <= file_row_number < group[1] + group[2]:
            return group
    total = groups[-1][1] + groups[-1][2] if groups else 0
    raise HfEdgarError(f"row_out_of_range:{file_row_number}>={total}")


def plan_row_group(footer, file_row_number, columns, max_bytes):
    """Find the row group holding the row; refuse if its requested chunks exceed max_bytes.

    ``footer`` rows: row_group_id, row_group_num_rows, path_in_schema, total_compressed_size
    (DuckDB parquet_metadata). Sizes are compressed column-chunk bytes, which DuckDB reads in full
    for any selected row in that group; HTTP overhead and footer reads are not counted.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise HfEdgarError("bad_max_bytes")
    return _group_plan(_find_group(_footer_groups(footer), file_row_number), columns, max_bytes)


def plan_row_groups(footer, file_row_numbers, columns, max_group_bytes, max_total_bytes):
    """Plan one read of several rows: each distinct row group is counted once, groups are summed.

    Every group must fit max_group_bytes and the sum must fit max_total_bytes. Like
    plan_row_group this is a preflight estimate of column chunks, not a network cap.
    """
    for value in (max_group_bytes, max_total_bytes):
        if type(value) is not int or value <= 0:
            raise HfEdgarError("bad_max_bytes")
    if not file_row_numbers or len(set(file_row_numbers)) != len(file_row_numbers):
        raise HfEdgarError("row_numbers_empty_or_duplicate")
    groups = _footer_groups(footer)
    by_id, rows = {}, {}
    for number in file_row_numbers:
        group = _find_group(groups, number)
        if group[0] not in by_id:
            by_id[group[0]] = _group_plan(group, columns, max_group_bytes)
        rows[number] = group[0]
    total = sum(plan["total_compressed_bytes"] for plan in by_id.values())
    if total > max_total_bytes:
        raise HfEdgarError(f"row_groups_too_large:{total}>{max_total_bytes}")
    return {"row_groups": [by_id[k] for k in sorted(by_id)], "row_to_group": rows,
            "total_compressed_bytes": total, "max_group_bytes": max_group_bytes,
            "max_total_bytes": max_total_bytes,
            "limit_kind": "preflight_column_chunk_estimate_not_network_cap"}


def group_targets(targets, reference_rows):
    """Validate an explicit target list (<= MAX_GROUP_TARGETS) against the reference rows.

    Each target is {accession, cik, file_row_number}; repeated accessions or row numbers and
    unknown keys are refused. Returns [(target, reference, primary_document)].
    """
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_GROUP_TARGETS:
        raise HfEdgarError(f"targets_count:1..{MAX_GROUP_TARGETS}")
    result = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"accession", "cik", "file_row_number"}:
            raise HfEdgarError(f"bad_target:{target!r}")
        if type(target["file_row_number"]) is not int or target["file_row_number"] < 0:
            raise HfEdgarError(f"bad_row_number:{target!r}")
        reference = reference_from_rows(reference_rows, target["accession"], target["cik"])
        primary = next(row for row in reference_rows
                       if row.get("accessionNumber") == target["accession"]
                       and row.get("cik") == target["cik"]).get("primaryDocument")
        result.append((target, reference, primary))
    for key in ("accession", "file_row_number"):
        values = [t[key] for t, _, _ in result]
        if len(set(values)) != len(values):
            raise HfEdgarError(f"duplicate_target_{key}")
    return result


def check_primary_document(documents, primary_document, data):
    """Compare metadata_documents with the reference primaryDocument and exported length.

    Only a single metadata entry whose filename equals primaryDocument is comparable; its
    secsgml_size_bytes must equal the exported byte length. This is dataset-internal
    consistency, not an independent SEC check and says nothing about other attachments.
    """
    if not documents.get("present") or not documents.get("json"):
        return {"status": "not_comparable", "reason": "metadata_documents_absent_or_not_json"}
    items = [i for i in (documents["parsed"] if isinstance(documents["parsed"], list)
                         else [documents["parsed"]]) if isinstance(i, dict)]
    if not isinstance(primary_document, str) or not primary_document:
        return {"status": "not_comparable", "reason": "reference_primary_document_missing"}
    hits = [i for i in items if i.get("filename") == primary_document]
    names = [i.get("filename") for i in items]
    if len(hits) != 1:
        raise HfEdgarError(f"primary_document_not_unique_in_metadata:{primary_document!r}:{names}")
    size = hits[0].get("secsgml_size_bytes")
    if type(size) is not int:
        return {"status": "not_comparable", "reason": f"secsgml_size_bytes:{size!r}",
                "filename": primary_document}
    if size != len(data):
        raise HfEdgarError(f"primary_document_size_mismatch:{size}!={len(data)}")
    return {"status": "consistent_within_dataset", "filename": primary_document,
            "secsgml_size_bytes": size, "exported_bytes": len(data), "other_metadata_files": len(items) - 1,
            "scope": "dataset metadata vs reference primaryDocument and exported length; "
                     "not independent SEC verification; attachments not claimed"}


def _write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def _prepare_export(out_dir, origin, reference, row, plan):
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
    return target, record, data, provenance


def _write_export(target, record, data, provenance):
    _write_new(target, data)
    _write_new(record, (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode())


def export_content(out_dir, origin, reference, row, plan):
    """Write the content field verbatim as UTF-8 plus a provenance record; never overwrite."""
    prepared = _prepare_export(out_dir, origin, reference, row, plan)
    _write_export(*prepared)
    return prepared[3]


def export_group(out_dir, origins, checked_targets, rows, plan):
    """Export several rows of one shard; every row is validated before any byte is written.

    ``checked_targets`` comes from group_targets, ``origins`` maps row number to source and
    ``rows`` is the single content query result. Missing, extra or repeated rows, identity
    mismatches, primary-document inconsistencies and existing outputs all refuse the whole
    group. Files are written afterwards; a write error can still leave a partial directory,
    which is why the CLI writes its group receipt last.
    """
    numbers = [row.get("file_row_number") for row in rows]
    if any(type(number) is not int for number in numbers):
        raise HfEdgarError(f"content_rows_bad_number:{numbers!r}")
    wanted = [target["file_row_number"] for target, _, _ in checked_targets]
    if len(set(numbers)) != len(numbers) or sorted(numbers) != sorted(wanted):
        raise HfEdgarError(f"content_rows_mismatch:got={sorted(numbers)}"
                           f",want={sorted(wanted)}")
    by_number = {row["file_row_number"]: row for row in rows}
    prepared = []
    for target, reference, primary in checked_targets:
        number = target["file_row_number"]
        group_plan = next(g for g in plan["row_groups"]
                          if g["row_group_id"] == plan["row_to_group"][number])
        item = _prepare_export(out_dir, origins[number], reference, by_number[number],
                               {**group_plan, "shared_group_read": True})
        item[3]["primary_document_check"] = check_primary_document(
            item[3]["metadata"]["parsed"]["documents"], primary, item[2])
        prepared.append(item)
    for item in prepared:
        _write_export(*item)
    return [item[3] for item in prepared]


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

    def full_rows(self, url, row_numbers):
        """One parameterised query for the listed rows' content and metadata (never text)."""
        cols = ", ".join(f'"{c}"' for c in CONTENT_COLUMNS)
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
