"""Controlled CLI for TeraflopAI/SEC-EDGAR at the pinned revision.

Subcommands (none starts a full-corpus job):
  list-shards  list one form's Parquet paths from the saved manifest (offline);
  scan         serial metadata scan of at most --max-shards shards, one receipt per shard,
               >=1 s between network shards, stops at the first error;
  locate       find one accession in one shard and check it against the reference row;
  extract      footer preflight, then read one explicitly located row's content and save it;
  extract-group  read up to 20 explicitly located rows of one shard with one content query,
               after checking all identities and the summed per-row-group preflight.

Output never overwrites existing files. Network/read failures leave failure-*.json;
validation failures are reported on stderr. Both exit 1.
"""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import time

from quant_rsi.adapters import hf_edgar as hf

MAX_SHARDS = 20


def _manifest(path):
    return hf.load_manifest(Path(path).read_bytes())


def _references(path):
    data = Path(path).read_bytes()
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    return rows, {"file": str(path), "sha256": hf.sha256(data), "rows": len(rows)}


def _emit(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _own_dir(directory):
    """Refuse directories holding JSON not written by this adapter (e.g. another scan)."""
    directory = Path(directory)
    for item in directory.glob("*.json") if directory.exists() else ():
        try:
            schema = json.loads(item.read_bytes()).get("schema")
        except (ValueError, AttributeError):
            schema = None
        if schema != hf.SCHEMA:
            raise hf.HfEdgarError(f"foreign_file_in_output_dir:{item}")
    return directory


def _rows_with_filer(rows):
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["filer_parsed"] = json.loads(row["metadata_filer"])
        except (TypeError, ValueError) as error:
            item["filer_parse_error"] = str(error)
        result.append(item)
    return result


def cmd_list(args):
    paths = hf.shards(_manifest(args.manifest), args.form)
    print(json.dumps({"form": args.form, "count": len(paths), "paths": paths}, indent=2))
    return 0


def cmd_scan(args, reader_factory):
    manifest = _manifest(args.manifest)
    if not 1 <= args.max_shards <= MAX_SHARDS or args.interval < 1 or args.start < 0:
        raise hf.HfEdgarError("max_shards_or_interval_out_of_range")
    if date.fromisoformat(args.cutoff).isoformat() != args.cutoff:
        raise hf.HfEdgarError("cutoff_requires_iso_date")
    rows, ref_info = _references(args.reference)
    references = {}
    for row in rows:
        if row.get("filingDate", "") <= args.cutoff and row.get("form") in args.reference_form:
            references.setdefault(row["accessionNumber"], []).append(row)
    targets = sorted(references)
    targets_sha256 = hashlib.sha256("\n".join(targets).encode()).hexdigest()
    out = _own_dir(args.receipts)
    paths = hf.shards(manifest, args.form)[args.start:args.start + args.max_shards]
    reader = None
    for path in paths:
        receipt_path = out / (Path(path).stem + ".json")
        if hf.cached_receipt(receipt_path, path, targets_sha256, ref_info["sha256"],
                             manifest["manifest_sha256"]):
            print(json.dumps({"path": path, "status": "cached"}), flush=True)
            continue
        if receipt_path.exists():
            raise hf.HfEdgarError(f"receipt_exists_not_reusable:{receipt_path}")
        url = hf.resolve_url(manifest, path)
        started = time.monotonic()
        try:
            reader = reader or reader_factory(args.timeout, args.retries)
            accessions = reader.accessions(url)
            hits = [number for accession, number in accessions if accession in references]
            matched = _rows_with_filer(reader.metadata(url, hits))
        except Exception as error:
            failure = hf.write_failure(out, Path(path).stem, {
                "revision": hf.REVISION, "path": path, "url": url, "error": repr(error),
                "elapsed": time.monotonic() - started})
            print(json.dumps({"path": path, "status": "error", "failure": str(failure)}))
            return 1
        _emit(receipt_path, {
            "schema": hf.SCHEMA, "status": "ok", "revision": hf.REVISION, "path": path,
            "url": url, "manifest_sha256": manifest["manifest_sha256"], "reference": ref_info,
            "targets_sha256": targets_sha256, "targets": len(targets), "rows": len(accessions),
            "matched": matched, "identity_checked": False,
            "elapsed": time.monotonic() - started})
        print(json.dumps({"path": path, "status": "ok", "rows": len(accessions),
                          "matched": len(matched)}), flush=True)
        time.sleep(args.interval)
    return 0


def _reference(args):
    rows, ref_info = _references(args.reference)
    return hf.reference_from_rows(rows, args.accession, args.cik), ref_info


def cmd_locate(args, reader_factory):
    manifest = _manifest(args.manifest)
    reference, ref_info = _reference(args)
    url = hf.resolve_url(manifest, args.path)
    out = _own_dir(args.out)
    reader = reader_factory(args.timeout, args.retries)
    try:
        hits = [n for a, n in reader.accessions(url) if a == reference["accession"]]
        rows = reader.metadata(url, hits)
        row, checked = hf.unique_match(rows, reference)
        footer = reader.footer(url)
    except Exception as error:
        hf.write_failure(out, f"locate-{args.accession}", {
            "path": args.path, "url": url, "reference": reference, "error": repr(error)})
        raise
    try:
        preflight = hf.plan_row_group(footer, row["file_row_number"], hf.CONTENT_COLUMNS,
                                      args.max_row_group_bytes)
    except hf.HfEdgarError as error:
        preflight = {"refused": str(error)}
    _emit(out / f"location-{args.accession}.json", {
        "schema": hf.SCHEMA, "status": "located_metadata_only",
        "source": hf.source(manifest, args.path, row["file_row_number"]),
        "reference": reference, "reference_file": ref_info, "metadata": checked,
        "content_preflight": preflight, "footer": footer, "content_read": False,
        "accepted_at": None, "ready": False})
    print(json.dumps({"located": row["file_row_number"], "preflight": preflight}, indent=2))
    return 0


def cmd_extract(args, reader_factory):
    manifest = _manifest(args.manifest)
    reference, ref_info = _reference(args)
    origin = hf.source(manifest, args.path, args.row_number)
    out = _own_dir(args.out)
    stage = "footer"
    try:
        reader = reader_factory(args.timeout, args.retries)
        plan = hf.plan_row_group(reader.footer(origin["url"]), args.row_number,
                                 hf.CONTENT_COLUMNS, args.max_row_group_bytes)
        stage = "content"
        rows = reader.full_row(origin["url"], args.row_number, reference["accession"])
        if len(rows) != 1:
            raise hf.HfEdgarError(f"located_rows:{len(rows)}")
        origin = dict(origin, reference_file=ref_info)
        provenance = hf.export_content(out, origin, reference, rows[0], plan)
    except Exception as error:
        hf.write_failure(out, f"extract-{args.accession}", {
            "stage": stage, "source": origin, "reference": reference, "error": repr(error)})
        raise
    print(json.dumps(provenance["content"], indent=2))
    return 0


def cmd_extract_group(args, reader_factory):
    manifest = _manifest(args.manifest)
    rows, ref_info = _references(args.reference)
    targets_data = Path(args.targets).read_bytes()
    checked = hf.group_targets(json.loads(targets_data), rows)
    numbers = [target["file_row_number"] for target, _, _ in checked]
    origins = {n: dict(hf.source(manifest, args.path, n), reference_file=ref_info) for n in numbers}
    url = hf.resolve_url(manifest, args.path)
    out = _own_dir(args.out)
    if bool(args.local_parquet) != bool(args.transfer_receipt):
        raise hf.HfEdgarError("local_parquet_and_transfer_receipt_required_together")
    for _, reference, _ in checked:
        for name in (f"{reference['accession']}.content",
                     f"{reference['accession']}.content.provenance.json"):
            if (out / name).exists():
                raise hf.HfEdgarError(f"output_exists:{out / name}")
    stage, started, plan = "metadata", time.monotonic(), None
    try:
        if args.local_parquet:
            from quant_rsi.adapters.local_parquet import LocalParquetReader
            reader = LocalParquetReader(args.local_parquet, args.transfer_receipt, args.path)
            origins = {n: dict(origin, transport=reader.transport_evidence)
                       for n, origin in origins.items()}
        else:
            reader = reader_factory(args.timeout, args.retries)
        meta = reader.metadata(url, numbers)
        if sorted(r.get("file_row_number") for r in meta) != sorted(numbers):
            raise hf.HfEdgarError(f"metadata_rows_mismatch:{[r.get('file_row_number') for r in meta]}")
        by_number = {r["file_row_number"]: r for r in meta}
        for target, reference, _ in checked:
            hf.check_metadata(by_number[target["file_row_number"]], reference)
        stage = "footer"
        plan = hf.plan_row_groups(reader.footer(url), numbers, hf.CONTENT_COLUMNS,
                                  args.max_row_group_bytes, args.max_total_bytes)
        stage = "content"
        content_rows = reader.full_rows(url, numbers)
        stage = "export"
        records = hf.export_group(out, origins, checked, content_rows, plan)
    except Exception as error:
        present = [f"{reference['accession']}.content" for _, reference, _ in checked
                   if (out / f"{reference['accession']}.content").exists()]
        hf.write_failure(out, f"group-{Path(args.path).stem}", {
            "stage": stage, "path": args.path, "url": url, "targets_sha256": hf.sha256(targets_data),
            "row_numbers": numbers, "plan": plan, "error": repr(error),
            "elapsed": time.monotonic() - started, "content_written": bool(present),
            "content_files_present": present, "export_stage_entered": stage == "export"})
        raise
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    summary = [{"accession": r["reference"]["accession"], "file_row_number":
                r["source"]["file_row_number"], "file": r["content"]["file"],
                "bytes": r["content"]["bytes"], "sha256": r["content"]["sha256"],
                "primary_document_check": r["primary_document_check"]["status"]} for r in records]
    _emit(out / f"group-{Path(args.path).stem}-{stamp}.json", {
        "schema": hf.SCHEMA, "status": "ok", "revision": hf.REVISION, "path": args.path,
        "url": url, "manifest_sha256": manifest["manifest_sha256"], "reference": ref_info,
        "targets": {"file": str(args.targets), "sha256": hf.sha256(targets_data)},
        "plan": plan, "content_queries": 1, "exported": summary,
        "elapsed": time.monotonic() - started, "accepted_at": None, "ready": False})
    print(json.dumps({"status": "ok", "exported": summary}, indent=2))
    return 0


def _duckdb_reader(timeout, retries):
    return hf.DuckDBReader(timeout=timeout, retries=retries)


def parser():
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = root.add_subparsers(dest="command", required=True)

    def network(command):
        command.add_argument("--timeout", type=int, default=30, help="seconds per HTTP request")
        command.add_argument("--retries", type=int, default=1)

    listing = sub.add_parser("list-shards")
    listing.add_argument("--manifest", required=True)
    listing.add_argument("--form", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--manifest", required=True)
    scan.add_argument("--form", required=True)
    scan.add_argument("--reference", required=True, help="submissions-index JSONL")
    scan.add_argument("--reference-form", nargs="+", required=True)
    scan.add_argument("--cutoff", required=True, help="max filingDate, YYYY-MM-DD")
    scan.add_argument("--receipts", required=True)
    scan.add_argument("--start", type=int, default=0)
    scan.add_argument("--max-shards", type=int, required=True)
    scan.add_argument("--interval", type=float, default=1.0)
    network(scan)
    for name in ("locate", "extract"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", required=True)
        command.add_argument("--path", required=True)
        command.add_argument("--reference", required=True)
        command.add_argument("--accession", required=True)
        command.add_argument("--cik", type=int, required=True)
        command.add_argument("--out", required=True)
        command.add_argument("--max-row-group-bytes", type=int,
                             default=hf.DEFAULT_MAX_ROW_GROUP_BYTES)
        network(command)
        if name == "extract":
            command.add_argument("--row-number", type=int, required=True)
    group = sub.add_parser("extract-group")
    group.add_argument("--manifest", required=True)
    group.add_argument("--path", required=True)
    group.add_argument("--reference", required=True)
    group.add_argument("--targets", required=True,
                       help="JSON list of {accession, cik, file_row_number}, at most 20")
    group.add_argument("--out", required=True)
    group.add_argument("--local-parquet", help="explicit complete local Parquet; no network reads")
    group.add_argument("--transfer-receipt", help="binds local bytes, mirror URL, SHA-256 and size")
    group.add_argument("--max-row-group-bytes", type=int, default=hf.DEFAULT_MAX_ROW_GROUP_BYTES)
    group.add_argument("--max-total-bytes", type=int, required=True,
                       help="sum over distinct row groups; preflight estimate, not a network cap")
    network(group)
    return root


def main(argv=None, reader_factory=_duckdb_reader):
    args = parser().parse_args(argv)
    try:
        if args.command == "list-shards":
            return cmd_list(args)
        return {"scan": cmd_scan, "locate": cmd_locate, "extract": cmd_extract,
                "extract-group": cmd_extract_group}[args.command](args, reader_factory)
    except Exception as error:
        print(json.dumps({"status": "error", "error": repr(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
