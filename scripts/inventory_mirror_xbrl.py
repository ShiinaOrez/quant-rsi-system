"""Build a hash-verified local XBRL inventory from an issuer mirror queue, without network."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.archive_sources import Store  # noqa: E402
from scripts.issuer_mirror import load_queue  # noqa: E402
from quant_rsi.adapters.inline_xbrl import ExtractionError, Limits, read_zip  # noqa: E402
from quant_rsi.adapters.xbrl_instance import classify_members  # noqa: E402


def inventory(root, symbol):
    store = Store(root)
    catalog, rows, problems = store.catalog(), [], []
    for item in load_queue(store, symbol):
        if item["role"] != "xbrl_zip":
            continue
        row = {k: item[k] for k in ("accession", "form", "filing_date", "url")}
        row.update(inline_members=[], instance_members=[], members=[])
        record = catalog.get(item["key"])
        if not record or store.read(record["sha256"]) is None:
            row["classification_error"] = "cached_blob_missing_or_corrupt"
        else:
            path = store.blob_path(record["sha256"])
            row.update(sha256=record["sha256"], path=str(path))
            try:
                issues = []
                _, members = read_zip(path, Limits(), issues)
                row["members"] = list(members)
                row["inline_members"], row["instance_members"] = classify_members(members)
                if not row["inline_members"] and not row["instance_members"]:
                    row["classification_error"] = "no_supported_xbrl_document"
                if issues:
                    row["classification_issues"] = issues
                    problems.append({"accession": item["accession"], "issues": issues})
            except ExtractionError as error:
                row["classification_error"] = str(error)
        if "classification_error" in row:
            problems.append({"accession": item["accession"], "error": row["classification_error"]})
        rows.append(row)
    return {"schema": "mirror-xbrl-inventory.v1", "symbol": symbol, "zip_count": len(rows),
            "inline_count": sum(bool(r["inline_members"]) for r in rows),
            "instance_count": sum(bool(r["instance_members"]) for r in rows),
            "problems": problems, "filings": rows, "benchmark_ready": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", choices=["INTC"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("inventory output already exists")
    result = inventory(args.root, args.symbol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "filings"}))
    raise SystemExit(int(bool(result["problems"])))
