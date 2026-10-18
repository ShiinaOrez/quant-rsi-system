"""Plan/fetch/parse one archived feed under an explicit TOML transport policy.

Only fetch performs network requests. The caller supplies the archived payload;
its whole SHA-256, SHA-1 CDX digest and length must match the policy before use.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tomllib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_rsi.adapters.archived_q4 import plan_capture  # noqa: E402
from quant_rsi.adapters import q4_filings as q4  # noqa: E402
from scripts.archive_sources import Store, now_utc  # noqa: E402
from scripts.q4_sources import (SampleFetcher, BudgetExceeded, _load, _write,
                                _read_jsonl, cmd_parse)  # noqa: E402


def make_plan(capture, policy_path, reference_dir, out, through):
    policy_bytes = Path(policy_path).read_bytes()
    policy = tomllib.loads(policy_bytes.decode())
    with Path(capture).open("rb") as stream:
        raw = stream.read((8 << 20) + 1)
    if len(raw) > 8 << 20:
        raise q4.FeedError("capture_exceeds_8MiB_limit")
    plan = plan_capture(raw, policy, through=through)
    plan["source"]["policy_file_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
    symbol = plan["symbol"]
    refs = q4.reference_rows(_read_jsonl(Path(reference_dir) / f"{symbol}.jsonl"),
                             plan["cik"], through=through)
    check = q4.cross_check(plan, refs)
    _write(out / f"plan-{symbol}.json.gz", plan)
    _write(out / f"crosscheck-{symbol}.json", check)
    return {"symbol": symbol, "source": plan["source"], "stats": plan["stats"],
            "transport_counts": plan["transport_counts"], "crosscheck": check["stats"], "ready": False}


def fetch_zips(store, out, symbol, max_requests=12):
    plan = _load(out / f"plan-{symbol}.json.gz")
    if (plan["source"].get("schema") != "archived-q4-https.v1"
            or plan.get("symbol") != symbol or plan.get("cik") != q4.issuer(symbol)["cik"]
            or plan["source"].get("issuer") != symbol):
        raise q4.FeedError("explicit_transport_plan_required")
    fetcher = SampleFetcher(store, max_requests=max_requests)
    results = []
    for row in sorted(plan["rows"], key=lambda r: (r["filing_date"], r["row_index"])):
        for doc in row["documents"]:
            if doc["document_type"] != "XBRL" or not doc["fetchable"]:
                continue
            fid = row["raw"]["FilingId"]
            expected = doc["url"]
            if doc["transport_operation"] == "http_to_https_same_host_and_path" and expected.startswith("http://"):
                expected = "https://" + expected[len("http://"):]
            elif doc["transport_operation"] != "none":
                raise q4.FeedError("unexpected_transport_operation")
            if expected != doc["fetch_url"]:
                raise q4.FeedError("transport_candidate_does_not_match_observed_url")
            meta = {"symbol": symbol, "filing_id": fid, "document_type": "XBRL",
                    "observed_url": doc["url"], "transport_operation": doc["transport_operation"],
                    "feed_sha256": plan["source_sha256"],
                    "policy_sha256": plan["source"]["policy_file_sha256"]}
            key = f"q4-xbrl-{symbol}-{fid}"
            try:
                rec = fetcher.get(key, doc["fetch_url"], meta)
            except (BudgetExceeded, q4.FeedError) as err:
                rec = {"key": key, "ok": False, "error": f"{type(err).__name__}:{err}"}
            results.append({k: rec.get(k) for k in ("key", "ok", "status", "bytes", "sha256",
                                                   "error", "error_detail", "cache_hit")})
    report = {"requests_this_run": fetcher.requests, "bytes_this_run": fetcher.bytes,
              "stopped": fetcher.stopped, "results": results, "ready": False}
    _write(out / f"fetch-{now_utc().replace(':', '')}.json", report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("plan", "fetch", "parse"))
    ap.add_argument("--policy", default="configs/data-sources/aapl-q4-20200730.toml")
    ap.add_argument("--capture")
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--reference", default="artifacts/benchmark-quality/public-index/import-2026-09-30-v2")
    ap.add_argument("--root", default="artifacts/benchmark-quality/aapl-archive-https-v1")
    ap.add_argument("--through", default="2024-10-04")
    ap.add_argument("--max-requests", type=int, default=12)
    args = ap.parse_args()
    root = Path(args.root)
    out = root / "plans"
    if args.command == "plan":
        if not args.capture:
            ap.error("plan requires --capture with the saved archived feed payload")
        report = make_plan(args.capture, args.policy, args.reference, out, args.through)
    elif args.command == "fetch":
        report = fetch_zips(Store(root / "store"), out, args.symbol, args.max_requests)
    else:
        report = cmd_parse(Store(root / "store"), out, args.reference)
    print(json.dumps(report, indent=1, default=str))


if __name__ == "__main__":
    main()
