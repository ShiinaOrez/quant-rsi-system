"""Drain an existing mirror queue serially; checkpoint after each bounded batch."""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
from pathlib import Path
import time

from scripts.issuer_mirror import MIRRORS, arch, cmd_coverage, cmd_fetch, make_fetcher


def materialize(root: Path, symbol: str, batch_size: int, max_seconds: int, interval: float = 3):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "materialize.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = arch.Store(root)
        receipt_path = root / "mirror" / symbol / "materialize-receipt.json"
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            if previous["reason"] == "host_blocked_stop":
                retry_at = datetime.fromisoformat(previous["at"]).timestamp() + 300
                for failure in store.last_failures().values():
                    value = failure.get("retry_after")
                    if failure.get("status") != 403 or value is None:
                        continue
                    try:
                        expiry = (datetime.fromisoformat(failure["at"]).timestamp() + float(value)
                                  if str(value).isdigit() else parsedate_to_datetime(value).timestamp())
                        retry_at = max(retry_at, expiry)
                    except (ValueError, TypeError, OverflowError):
                        pass
                remaining = retry_at - datetime.now(timezone.utc).timestamp()
                if remaining > 0:
                    raise ValueError(f"source cooldown active: retry in {int(remaining) + 1}s")
        fetcher = make_fetcher(store, timeout=20, retries=2, interval=interval)
        log = root / "materialize-progress.jsonl"
        started = time.monotonic()
        batches, downloaded, failed = 0, 0, 0
        reason = "time_budget"
        while time.monotonic() - started < max_seconds:
            result = cmd_fetch(fetcher, symbol, batch_size, include_images=True)
            batches += 1
            downloaded += result["fetched"]
            failed += result["failed"]
            progress = {"at": arch.now_utc(), "symbol": symbol, "batch": batches,
                        "downloaded_this_run": downloaded, "failed_this_run": failed,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "last_batch": {k: v for k, v in result.items() if k != "items"}}
            arch.Store.append(log, progress)
            print(json.dumps(progress), flush=True)
            if result["blocked"]:
                reason = "host_blocked_stop"
                break
            if result["processed"] == 0:
                reason = "no_more_unattempted_files"
                break
        coverage = cmd_coverage(store, MIRRORS[symbol])
        target = root / "mirror" / symbol
        target.mkdir(parents=True, exist_ok=True)
        (target / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
        receipt = {"at": arch.now_utc(), "reason": reason, "batches": batches,
                   "request_interval_seconds": interval,
                   "downloaded_this_run": downloaded, "failed_this_run": failed,
                   "files": {k: v for k, v in coverage.get("files", {}).items() if k != "fetched_items"},
                   "benchmark_ready": False, "gaps": coverage["gaps"]}
        (target / "materialize-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({"receipt": str(target / "materialize-receipt.json"), "reason": reason,
                          "fetched_total": receipt["files"].get("fetched"),
                          "pending": receipt["files"].get("pending"), "benchmark_ready": False}), flush=True)
        return reason == "no_more_unattempted_files" and not receipt["files"].get("failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", default="INTC", choices=sorted(MIRRORS))
    parser.add_argument("--batch-size", type=int, choices=range(1, 51), default=25)
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--interval", type=float, default=3, help="minimum seconds between requests; >=3")
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 7200:
        parser.error("time budget must be in [1, 7200] seconds")
    if not math.isfinite(args.interval) or args.interval < 3:
        parser.error("bulk materialization requires at least three seconds between requests")
    raise SystemExit(0 if materialize(args.root, args.symbol, args.batch_size, args.max_seconds, args.interval) else 1)
