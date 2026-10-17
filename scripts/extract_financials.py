"""Extract XBRL facts from local SEC ``*-xbrl.zip`` files (no network).

Each ZIP goes through xbrl_instance.extract_any_zip: inline-only ZIPs use the inline XBRL 1.1
extractor, ZIPs with traditional XBRL 2.1 instance documents use the instance extractor, and
ZIPs mixing both are refused. Accession and source URL come from a receipts JSON (entries with
url, name, sha256) or the ZIP inventory's filings, matched by the ZIP's sha256; a mismatch
refuses the file. Single-file mode takes them explicitly.

  extract_financials.py --receipts R.json --zip A.zip [--zip B.zip ...] --out-dir DIR
  extract_financials.py --inventory INV.json --select instance|inline|all --out-dir DIR [--summary-only]
  extract_financials.py --zip A.zip --accession 0000050863-24-000076 --source-url URL --out-dir DIR
  [--available-at 2024-04-26T16:05:00-04:00 --available-at-basis "SEC acceptance datetime"]

Writes per accession facts-<acc>.json and issues-<acc>.json, then summary.json and summary.md
with every Assets, NetIncomeLoss and NetCashProvidedByUsedInOperatingActivities[ContinuingOperations] fact (each
period and context), a few equity lines, and cross-filing differences. Output never claims
task readiness; available_at stays null unless both options are passed.
"""
import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quant_rsi.adapters import inline_xbrl as ix  # noqa: E402
from quant_rsi.adapters import xbrl_instance as xi  # noqa: E402

# The ContinuingOperations CFO concept is what Intel tagged in 2013-2018; it is listed as its own
# concept, never merged with NetCashProvidedByUsedInOperatingActivities.
KEY_CONCEPTS = ("Assets", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
                "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
CONTEXT_CONCEPTS = ("LiabilitiesAndStockholdersEquity", "StockholdersEquity",
                    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "ProfitLoss")
# us-gaap namespaces seen in SEC filings: fasb.org/us-gaap/YYYY (2019+), fasb.org/us-gaap/YYYY-01-31
# (2011-2018 releases) and the XBRL US 2009 release.
US_GAAP = re.compile(r"^(http://fasb\.org/us-gaap/\d{4}(-\d{2}-\d{2})?|http://xbrl\.us/us-gaap/2009-01-31)$")


def _receipt(receipts, path):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    match = [r for r in receipts if r.get("sha256") == digest]
    if len(match) != 1:
        raise SystemExit(f"{path}: sha256 {digest} matches {len(match)} receipts; refusing")
    found = re.search(r"/(\d{10}-\d{2}-\d{6})/", match[0]["url"])
    if not found:
        raise SystemExit(f"{path}: receipt URL has no accession")
    return found.group(1), match[0]["url"], match[0]


def _billions(value):
    return None if value is None else f"{Decimal(value) / Decimal(10**9):,.3f}B"


def key_rows(result, concepts):
    rows = []
    for doc in result["documents"]:
        for fact in doc["facts"]:
            q = fact["concept"]
            if not (q["local_name"] in concepts and US_GAAP.match(q["namespace"] or "")):
                continue
            ctx = fact["context"] or {}
            dims = [f"{d['dimension']['qname']}={d['member']['qname'] if d['kind'] == 'explicit' else d['raw']}"
                    for d in (ctx.get("segment") or []) + (ctx.get("scenario") or [])]
            unit = fact["unit"] or {}
            rows.append({"accession": result["accession"], "concept": q["qname"], "namespace": q["namespace"],
                         "fact_id": fact["fact_id"], "context_ref": fact["context_ref"],
                         "period": ctx.get("period"), "dimensions": dims,
                         "unit": "/".join(["*".join(m["qname"] for m in unit.get("numerator", []))]
                                          + (["*".join(m["qname"] for m in unit["denominator"])]
                                             if unit.get("denominator") else [])),
                         "document_type": result.get("document_type"), "member": fact["source"]["member"],
                         "raw_text": fact["raw_text"], "scale": fact["scale"], "sign": fact["sign"],
                         "decimals": fact["decimals"], "precision": fact.get("precision"), "nil": fact["nil"], "format": (fact["format"] or {}).get("qname"),
                         "value": fact["value"], "billions": _billions(fact["value"]), "status": fact["status"],
                         "errors": fact["errors"], "hidden": fact["hidden"], "line": fact["source"]["line"],
                         "roles": [r["definition"] or r["role_uri"] for r in fact["presentation_roles"]]})
    return sorted(rows, key=lambda r: (r["concept"], json.dumps(r["period"], sort_keys=True), r["dimensions"],
                                       r["fact_id"] or ""))


def balance_identity(result):
    """Assets vs LiabilitiesAndStockholdersEquity per undimensioned instant. Only this one identity."""
    found = {}
    for row in key_rows(result, ("Assets", "LiabilitiesAndStockholdersEquity")):
        if not row["dimensions"] and row["value"] is not None:
            found.setdefault(json.dumps(row["period"]), {}).setdefault(row["concept"].split(":")[1], set()).add(row["value"])
    out = []
    for period, values in sorted(found.items()):
        a, le = values.get("Assets", set()), values.get("LiabilitiesAndStockholdersEquity", set())
        out.append({"period": json.loads(period), "assets": sorted(a), "liabilities_and_equity": sorted(le),
                    "equal": len(a) == 1 and a == le})
    return out


def _period_text(period):
    if not period:
        return "?"
    return period.get("instant") or f"{period.get('start')}..{period.get('end')}" if period["type"] != "forever" else "forever"


def write_markdown(path, summary):
    lines = ["# XBRL key facts (machine-generated; values are as tagged, not validated statements)", "",
             "available_at/pit_ready/task_ready: " + json.dumps(summary["availability"]), ""]
    for acc, info in summary["filings"].items():
        members = "; ".join(f"`{m['member']}` sha256 `{m['member_sha256']}`" for m in info["documents"]) or "none"
        lines += [f"## {acc} ({info['document_type']})", "", f"zip sha256 `{info['zip_sha256']}`; documents {members}",
                  "", "counts: `" + json.dumps(_numeric_counts(info["counts"])) + "`", "",
                  "| concept | period | dims | ctx | fact | raw | scale/sign | decimals | value | ≈ | status |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in info["key_facts"] + info["context_facts"]:
            lines.append(f"| {r['concept']} | {_period_text(r['period'])} | {'; '.join(r['dimensions']) or '—'} | "
                         f"{r['context_ref']} | {r['fact_id']} | {r['raw_text']} | {r['scale']}/{r['sign'] or ''} | "
                         f"{r['decimals'] if r['precision'] is None else 'p=' + r['precision']} | "
                         f"{r['value']} | {r['billions']} | {r['status']} |")
        lines += ["", "balance identity (undimensioned): `" + json.dumps(info["balance_identity"]) + "`", ""]
    exact = summary["cross_filing_exact_qname"]
    lines += ["## cross-filing", "", f"exact QName: shared {exact['shared_keys']}, same {exact['same']}, "
              f"differs {len(exact['differs'])}.", "",
              "us-gaap local name across taxonomy years (convention, unverified):", "", "`" + json.dumps({k: v for k, v in summary["cross_filing"].items()
                                                       if k != "differs"}) + "`", ""]
    for d in summary["cross_filing"]["differs"]:
        lines.append(f"- {d['concept']} {d['context_signature']}: {json.dumps(d['values_by_accession'])}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _numeric_counts(counts):
    """Numeric fact counts under the extractor's own kind name (never relabelled as inline)."""
    return {kind: counts[kind] for kind in ("nonFraction", xi.NUMERIC) if kind in counts}


def _inventory_zips(inventory, select):
    out = []
    for filing in inventory["filings"]:
        if select == "instance" and not filing.get("instance_members"):
            continue
        if select == "inline" and not filing.get("inline_members"):
            continue
        out.append(filing["path"])
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", action="append", default=[])
    parser.add_argument("--receipts")
    parser.add_argument("--inventory", help="ZIP inventory JSON; its filings act as receipts")
    parser.add_argument("--select", choices=("instance", "inline", "all"),
                        help="with --inventory: take ZIP paths from the inventory")
    parser.add_argument("--summary-only", action="store_true", help="skip per-filing facts-<acc>.json")
    parser.add_argument("--accession")
    parser.add_argument("--source-url")
    parser.add_argument("--available-at")
    parser.add_argument("--available-at-basis")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    if sum(map(bool, (args.receipts, args.inventory, args.accession))) != 1:
        parser.error("use exactly one of --receipts, --inventory, --accession")
    if args.select and not args.inventory:
        parser.error("--select needs --inventory")
    receipts = json.loads(Path(args.receipts).read_text()) if args.receipts else None
    if args.inventory:
        inventory = json.loads(Path(args.inventory).read_text())
        receipts = inventory["filings"]
        if args.select:
            args.zip += _inventory_zips(inventory, args.select)
    if not args.zip:
        parser.error("no --zip given or selected")
    if args.accession and len(args.zip) != 1:
        parser.error("--accession needs exactly one --zip; use --receipts for several")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results, summary = [], {"availability": None, "filings": {}}
    for path in args.zip:
        if receipts is not None:
            accession, url, receipt = _receipt(receipts, path)
        else:
            accession, url, receipt = args.accession, args.source_url, None
        if receipt is not None and receipt.get("accession") not in (None, accession):
            raise SystemExit(f"{path}: receipt accession {receipt['accession']} != URL accession {accession}")
        result = xi.extract_any_zip(path, accession=accession, source_url=url, available_at=args.available_at,
                                    available_at_basis=args.available_at_basis)
        if receipt is not None:
            result["receipt"] = {k: receipt.get(k) for k in ("url", "name", "sha256", "bytes", "form", "filing_date",
                                                             "accepted_at", "coverage")}
        results.append(result)
        if not args.summary_only:
            (out / f"facts-{accession}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
        (out / f"issues-{accession}.json").write_text(json.dumps(
            {"accession": accession, "counts": result["counts"], "issues": result["issues"],
             "duplicates": [g for d in result["documents"] for g in d["duplicates"]],
             "refused_members": [m for m in result["zip"]["members"] if m.get("status") == "refused"]},
            indent=1, ensure_ascii=False) + "\n")
        summary["availability"] = {**result["availability"], "task_ready": result["task_ready"]}
        summary["filings"][accession] = {
            "document_type": result["document_type"], "form": (receipt or {}).get("form"),
            "filing_date": (receipt or {}).get("filing_date"), "zip_sha256": result["zip"]["sha256"],
            "documents": [{"member": d["member"], "member_sha256": d["member_sha256"]} for d in result["documents"]],
            "source_url": url, "counts": result["counts"], "key_facts": key_rows(result, KEY_CONCEPTS),
            "context_facts": key_rows(result, CONTEXT_CONCEPTS), "balance_identity": balance_identity(result)}
    summary["cross_filing_exact_qname"] = ix.compare_filings(results)
    summary["cross_filing"] = ix.compare_filings(results, match="us_gaap_local_name")
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")
    write_markdown(out / "summary.md", summary)
    print(json.dumps({acc: {"type": f["document_type"], "facts": f["counts"]["facts"],
                            "numeric": {k: v["by_status"] for k, v in _numeric_counts(f["counts"]).items()},
                            "issues": f["counts"]["issues_by_code"]} for acc, f in summary["filings"].items()},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
