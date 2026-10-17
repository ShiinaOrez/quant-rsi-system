"""Audit exact task windows against cached vendor responses, without inference.

Checks calendar coverage, not source licensing, full-history completeness,
financial availability or normalization accuracy. Never declares tasks ready.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import tomllib

from scripts.archive_sources import Store, parse_yahoo_chart, reconstruct_raw


def audit(archive: Path, tasks: Path):
    import exchange_calendars as xc

    store = Store(archive)
    catalog = store.catalog()
    charts = {}
    results = []
    for path in sorted(tasks.glob("*.toml")):
        task = tomllib.loads(path.read_text())
        symbol = task["instrument"]["symbol"]
        cutoff = datetime.fromisoformat(task["episode"]["as_of"])
        source = catalog.get(f"yahoo:chart:{symbol}")
        if source is None:
            results.append({"task_id": task["id"], "status": "source_missing", "benchmark_ready": False})
            continue
        if symbol not in charts:
            raw = store.read(source["sha256"])
            if raw is None:
                raise ValueError(f"{symbol}: missing or corrupt source")
            charts[symbol] = parse_yahoo_chart(raw)
        chart = charts[symbol]
        rows = {bar["session"]: bar for bar in chart["bars"]}
        if len(rows) != len(chart["bars"]):
            raise ValueError(f"{symbol}: duplicate session")
        calendar = xc.get_calendar("XNAS", start=(cutoff - timedelta(days=500)).date().isoformat(),
                                   end=(cutoff + timedelta(days=150)).date().isoformat())
        days = list(calendar.schedule.index.strftime("%Y-%m-%d"))
        cutoff_day = cutoff.date().isoformat()
        previous = [day for day in days if day <= cutoff_day][-252:]
        forward = [day for day in days if day > cutoff_day][:task["episode"]["horizon_sessions"]]
        missing = [day for day in previous + forward if day not in rows]
        invalid = [day for day in previous + forward if day in rows
                   and any(rows[day][field] is None or type(rows[day][field]) not in (int, float)
                           or not math.isfinite(rows[day][field]) or rows[day][field] <= 0
                           for field in ("open", "high", "low", "close"))]
        close_matches = (calendar.is_session(cutoff_day)
                         and calendar.session_close(cutoff_day).to_pydatetime() == cutoff)
        complete = (not missing and not invalid and len(previous) == 252
                    and len(forward) == task["episode"]["horizon_sessions"] and close_matches)
        normalized = reconstruct_raw(chart)
        results.append({"task_id": task["id"], "symbol": symbol,
                        "task_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "vendor_response_sha256": source["sha256"],
                        "first_session": forward[0], "terminal_session": forward[-1],
                        "warmup_sessions": len(previous), "forward_sessions": len(forward),
                        "cutoff_matches_close": close_matches, "missing_sessions": missing,
                        "invalid_ohlc_sessions": invalid, "price_window_coverage_pass": complete,
                        "forward_dividends": [d for d in normalized["dividends"] if d["ex_session"] in forward],
                        "forward_splits": [s for s in chart["splits"] if s["session"] in forward],
                        "benchmark_ready": False})
    if not results:
        raise ValueError("no tasks")
    return {"schema_version": "market_window_audit.v1", "calendar": "XNAS",
            "calendar_library_version": xc.__version__, "tasks": results,
            "tasks_audited": len(results),
            "passed_price_window_coverage": sum(row.get("price_window_coverage_pass", False) for row in results),
            "benchmark_ready": False,
            "limitations": ["252 warmup plus forward windows only; not full-history calendar certification",
                            "candidate split normalization not verified against independent source",
                            "financial archive, official pay dates and permitted use not certified"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, default=Path("benchmarks/tasks"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.archive, args.tasks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("tasks_audited", "passed_price_window_coverage", "benchmark_ready")}))
