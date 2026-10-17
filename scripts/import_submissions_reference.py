"""Offline, hash-pinned import of Ian D. Gow's SEC submissions reference release.

This creates private build metadata, never an agent-visible archive or a readiness
certificate. A supported timestamp is an upstream claim, not our independent
verification. Original filing bytes and attachments must be collected separately.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tomllib

from scripts.archive_sources import EXPECTED_CIK, PLAN_FORMS, FORM_VARIANTS
from scripts.archive_bounds import suite_bounds

DEFAULT_SOURCE = Path("configs/data-sources/sec-submissions-2026-09-30.toml")
DEFAULT_SUITE = Path("benchmarks/suites/nasdaq-diagnostic-v1.toml")
REQUIRED = {"cik", "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
            "form", "primaryDocument", "source_file", "timestamp_provenance",
            "timestamp_reference_id", "timestamp_interpretation"}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_release(manifest_path, filings_path, source):
    if digest(manifest_path) != source["manifest_sha256"]:
        raise ValueError("manifest checksum mismatch")
    manifest = json.loads(Path(manifest_path).read_bytes())
    if (manifest.get("version") != source["version"] or
            manifest.get("license") != source["license"] or
            manifest.get("population", {}).get("rows") != source["population_rows"]):
        raise ValueError("release identity or population mismatch")
    assets = [x for x in manifest["files"] if x["name"] == "filings.parquet"]
    if len(assets) != 1:
        raise ValueError("ambiguous or absent filings asset")
    asset = assets[0]
    if (asset["sha256"] != source["filings_sha256"] or
            asset["size_bytes"] != source["filings_size_bytes"]):
        raise ValueError("manifest asset differs from pinned source")
    if Path(filings_path).stat().st_size != asset["size_bytes"]:
        raise ValueError("filings size mismatch")
    if digest(filings_path) != asset["sha256"]:
        raise ValueError("filings checksum mismatch")
    return manifest


def assess_timestamp(row):
    """Report evidence class; never promote the reference into verified PIT data."""
    provenance = row.get("timestamp_provenance")
    value = row.get("acceptanceDateTime")
    if value is None:
        return "missing"
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("acceptanceDateTime must be timezone-aware")
    if provenance in ("unresolved_raw_as_eastern", "raw_missing", None, ""):
        return "unresolved"
    # Known upstream rules are enumerated after inspecting the pinned release.
    # A new provenance must be reviewed before it can be treated as supported.
    if provenance in {"sgml", "live_pair", "live_file_inferred",
                      "trusted_snapshot_eastern", "cross_cik_pair"}:
        return "upstream_supported_not_independently_verified"
    return "unknown_provenance"


def checked_row(row, expected_cik):
    if row.get("cik") != expected_cik:
        raise ValueError("issuer CIK mismatch")
    if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", row.get("accessionNumber") or ""):
        raise ValueError("invalid accession")
    if not isinstance(row.get("filingDate"), date):
        raise ValueError("missing filing date")
    result = dict(row)
    result["timestamp_evidence_status"] = assess_timestamp(row)
    result["independently_verified_publication_time"] = False
    return result


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def run(args):
    import duckdb
    source_bytes = args.source.read_bytes()
    source = tomllib.loads(source_bytes.decode())
    if args.output.exists():
        raise FileExistsError("use a new output directory; imports are immutable")
    manifest = validate_release(args.manifest, args.filings, source)
    bounds = suite_bounds(args.suite)
    through = date.fromisoformat(bounds["through"])
    with duckdb.connect() as con:
        con.execute("SET threads=2; SET memory_limit='2GB'; SET TimeZone='UTC'")
        schema = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(args.filings)]).fetchall()
        columns = [x[0] for x in schema]
        if not REQUIRED.issubset(columns):
            raise ValueError("required reference columns missing")
        if dict((x[0], x[1]) for x in schema)["acceptanceDateTime"] != "TIMESTAMP WITH TIME ZONE":
            raise ValueError("reference timestamp is not timezone-aware")
        total = con.execute("SELECT count(*) FROM read_parquet(?)", [str(args.filings)]).fetchone()[0]
        if total != manifest["population"]["rows"]:
            raise ValueError("Parquet population differs from manifest")
        summaries, outputs = {}, {}
        for symbol, cik in sorted(EXPECTED_CIK.items()):
            fetched = con.execute("SELECT * FROM read_parquet(?) WHERE cik=? ORDER BY filingDate, accessionNumber",
                                  [str(args.filings), cik]).fetchall()
            rows = [checked_row(dict(zip(columns, values)), cik) for values in fetched]
            ids = [x["accessionNumber"] for x in rows]
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate issuer/accession: {symbol}")
            if not rows:
                raise ValueError(f"no issuer rows: {symbol}")
            kept = [x for x in rows if x["filingDate"] <= through]
            planned = [x for x in kept if x["form"] in PLAN_FORMS]
            summaries[symbol] = {
                "cik": cik, "snapshot_rows": len(rows), "rows_through_suite_terminal": len(kept),
                "first_filing_date": rows[0]["filingDate"], "last_filing_date": rows[-1]["filingDate"],
                "planned_forms_rows": len(planned),
                "historical_form_variants": dict(Counter(x["form"] for x in kept if x["form"] in FORM_VARIANTS)),
                "planned_timestamp_evidence": dict(Counter(x["timestamp_evidence_status"] for x in planned)),
                "timestamp_provenance": dict(Counter(x["timestamp_provenance"] for x in kept)),
                "benchmark_ready": False,
            }
            # Keep complete snapshot privately, including post-terminal records.
            # Environment compilation must apply its own aware decision clock.
            outputs[symbol] = rows
    report = {"schema": "submissions-reference-import.v1", "source": source,
              "source_config_sha256": hashlib.sha256(source_bytes).hexdigest(),
              "suite_sha256": digest(args.suite), "suite_bounds": bounds,
              "imported_at": datetime.now(timezone.utc).isoformat(), "duckdb_version": duckdb.__version__,
              "issuers": summaries, "benchmark_ready": False,
              "scope": "private_build_metadata_only", "complete_original_documents": False,
              "identity_note": "CIKs pinned by benchmark; snapshot tickers are not historical identity evidence"}
    args.output.mkdir(parents=True)
    report["derived_outputs"] = {}
    for symbol, rows in outputs.items():
        output_path = args.output / f"{symbol}.jsonl"
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, default=json_value, sort_keys=True) + "\n")
        report["derived_outputs"][symbol] = {"filename": output_path.name,
                                             "sha256": digest(output_path),
                                             "size_bytes": output_path.stat().st_size}
    (args.output / "report.json").write_text(json.dumps(report, default=json_value, indent=2) + "\n")
    print(json.dumps(report["issuers"], default=json_value, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--filings", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args())
