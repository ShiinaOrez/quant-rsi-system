"""Cache full available vendor histories; never promotes a task to ready.

The CSV is yfinance output, not the original HTTP response or verified raw
transaction prices. It includes future rows and belongs in private environment
storage, never directly in the trader's tools.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import tomllib


def symbols_from_tasks(directory: Path):
    return sorted({tomllib.loads(path.read_text())["instrument"]["symbol"]
                   for path in directory.glob("*.toml")})


def freeze(root: Path, symbols: list[str], delay: float = 3):
    import yfinance as yf

    root.mkdir(parents=True, exist_ok=True)
    summaries = []
    for symbol in symbols:
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol):
            raise ValueError("invalid instrument identifier")
        target = root / symbol
        target.mkdir(exist_ok=True)
        csv = target / "vendor-history.csv"
        receipt_path = target / "receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["sha256"] != hashlib.sha256(csv.read_bytes()).hexdigest():
                raise ValueError(f"{symbol}: cached data integrity failure")
            summaries.append(receipt)
            print(json.dumps({"symbol": symbol, "cached": True, "rows": receipt["rows"]}), flush=True)
            continue
        if csv.exists():
            raise FileExistsError(f"{csv}: interrupted cache; preserve and use a new root")
        try:
            history = yf.Ticker(symbol).history(period="max", auto_adjust=False, back_adjust=False,
                                                actions=True, repair=False, keepna=True,
                                                rounding=False, raise_errors=True)
            if history.empty or not history.index.is_monotonic_increasing or history.index.has_duplicates:
                raise ValueError("empty, duplicated, or unordered history")
            required = {"Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"}
            if not required.issubset(history.columns):
                raise ValueError("missing required vendor columns")
            content = history.to_csv().encode()
            with csv.open("xb") as output:
                output.write(content)
            receipt = {
                "schema_version": "vendor_history_cache.v1", "symbol": symbol,
                "benchmark_ready": False, "sha256": hashlib.sha256(content).hexdigest(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "rows": len(history),
                "first_vendor_timestamp": history.index[0].isoformat(),
                "last_vendor_timestamp": history.index[-1].isoformat(),
                "timezone": str(history.index.tz), "library": "yfinance", "version": yf.__version__,
                "source": f"https://finance.yahoo.com/quote/{symbol}/history/",
                "representation": "library_output_csv_not_original_http_response",
                "request": {"period": "max", "auto_adjust": False, "back_adjust": False,
                            "actions": True, "repair": False, "keepna": True, "rounding": False},
                "split_events": int((history["Stock Splits"].fillna(0) != 0).sum()),
                "dividend_events": int((history["Dividends"].fillna(0) != 0).sum()),
                "missing_ohlcv_rows": int(history[["Open", "High", "Low", "Close", "Volume"]].isna().any(axis=1).sum()),
                "gaps": ["vendor current download; not historical vintages",
                         "historical OHLC may include later split adjustment; not verified raw execution prices",
                         "volume adjustment convention requires verification",
                         "official dividend pay dates and publication times not supplied",
                         "complete trading-calendar coverage not yet audited",
                         "provider terms and permitted use require review"],
                "agent_visible": False,
            }
            with receipt_path.open("x") as output:
                output.write(json.dumps(receipt, indent=2) + "\n")
            summaries.append(receipt)
            print(json.dumps({"symbol": symbol, "rows": len(history), "first": receipt["first_vendor_timestamp"],
                              "last": receipt["last_vendor_timestamp"]}), flush=True)
        except Exception as error:
            failure = {"symbol": symbol, "status": "failed", "error_type": type(error).__name__,
                       "message": str(error), "benchmark_ready": False}
            summaries.append(failure)
            print(json.dumps(failure), flush=True)
        time.sleep(delay)
    (root / "coverage.json").write_text(json.dumps({"benchmark_ready": False, "symbols": summaries}, indent=2) + "\n")
    return all(item.get("sha256") for item in summaries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, default=Path("benchmarks/tasks"))
    args = parser.parse_args()
    raise SystemExit(0 if freeze(args.root, symbols_from_tasks(args.tasks)) else 1)
