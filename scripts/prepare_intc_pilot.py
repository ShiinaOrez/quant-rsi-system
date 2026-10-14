"""Freeze a diagnostic subset; never declares the full financial archive ready."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone


def download(root: Path):
    """Refresh upstream data explicitly; existing frozen files are never overwritten."""
    import yfinance as yf
    import requests
    root.mkdir(parents=True, exist_ok=True)
    files = ("yahoo-history.csv", "q1_2024.html", "q2_2024.html")
    if any((root / name).exists() for name in files):
        raise FileExistsError("use an empty --root to download a new data vintage")
    history = yf.Ticker("INTC").history(start="2023-01-01", end="2024-11-01",
                                        auto_adjust=False, actions=True)
    if history.empty:
        raise ValueError("empty upstream price history")
    history.to_csv(root / files[0])
    for name, release, quarter in ((files[1], 1692, "first"), (files[2], 1704, "second")):
        url = (f"https://www.intc.com/news-events/press-releases/detail/{release}/"
               f"intel-reports-{quarter}-quarter-2024-financial-results")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        (root / name).write_bytes(response.content)


def build(root: Path):
    import pandas as pd
    import exchange_calendars as xc
    from bs4 import BeautifulSoup

    history = pd.read_csv(root / "yahoo-history.csv")
    history["session"] = pd.to_datetime(history["Date"], utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    history = history.set_index("session")
    calendar = xc.get_calendar("XNAS", start="2023-01-01", end="2024-12-31")
    schedule = calendar.schedule
    cutoff = datetime.fromisoformat("2024-07-11T16:00:00-04:00")
    assert schedule.loc["2024-07-11", "close"].to_pydatetime() == cutoff
    future = schedule.loc["2024-07-12":].iloc[:60]
    bars, observations = [], []
    closes = []
    for day, row in history.iterrows():
        if day not in schedule.index.strftime("%Y-%m-%d"):
            continue
        if day > future.index[-1].strftime("%Y-%m-%d"):
            break
        times = schedule.loc[day]
        assert row["Stock Splits"] == 0, "split handling not implemented"
        close = float(row["Close"])
        closes.append(close)
        changes = pd.Series(closes).diff()
        gains = changes.clip(lower=0)
        losses = -changes.clip(upper=0)
        # Wilder seed is an N-change arithmetic mean, then recursive updates.
        rsi = None
        if len(closes) >= 15:
            avg_gain = float(gains.iloc[1:15].mean())
            avg_loss = float(losses.iloc[1:15].mean())
            for i in range(15, len(closes)):
                avg_gain = (avg_gain * 13 + float(gains.iloc[i])) / 14
                avg_loss = (avg_loss * 13 + float(losses.iloc[i])) / 14
            rsi = 50 if avg_loss == avg_gain == 0 else (100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
        content = {"session": day, "close": close, "volume": int(row["Volume"]),
                   "sma20": sum(closes[-20:]) / 20 if len(closes) >= 20 else None,
                   "sma60": sum(closes[-60:]) / 60 if len(closes) >= 60 else None,
                   "rsi14": rsi,
                   "daily_volatility20": float(pd.Series(closes).pct_change().iloc[-20:].std(ddof=1)) if len(closes) >= 21 else None,
                   "last_20_closes": closes[-20:]}
        observations.append({"record_id": "bar-" + day, "available_at": times["close"].isoformat(),
                             "event_time": times["close"].isoformat(), "content": json.dumps(content),
                             "source": "Yahoo historical bars frozen via yfinance"})
        if day in future.index.strftime("%Y-%m-%d"):
            actions = []
            if float(row["Dividends"]):
                assert day == "2024-08-07" and float(row["Dividends"]) == .125, "unmapped dividend"
                actions.append({"type": "dividend_ex", "id": "intc-2024-q3", "amount_per_share": "0.125"})
            if day == "2024-09-03":
                actions.append({"type": "dividend_pay", "id": "intc-2024-q3"})
            bars.append({"opens_at": times["open"].isoformat(), "closes_at": times["close"].isoformat(),
                         "raw_open": str(row["Open"]), "raw_close": str(row["Close"]), "actions": actions})
    assert len(bars) == 60, "missing trading sessions"
    reports = [
        ("q1_2024", "2024-04-25T16:01:00-04:00", "https://www.intc.com/news-events/press-releases/detail/1692/intel-reports-first-quarter-2024-financial-results"),
        ("q2_2024", "2024-08-01T16:01:00-04:00", "https://www.intc.com/news-events/press-releases/detail/1704/intel-reports-second-quarter-2024-financial-results"),
    ]
    for name, published, url in reports:
        soup = BeautifulSoup((root / (name + ".html")).read_text(), "html.parser")
        article = soup.select_one("article.full-news-article")
        assert article is not None, "financial document body not found"
        text = article.get_text(" ", strip=True)
        relevant = [t.get_text(" ", strip=True) for t in article.find_all("table")
                    if "Total assets" in t.get_text() or "Net cash provided by" in t.get_text()]
        # Original documents remain frozen; only this deterministic excerpt is sent.
        excerpt = text[:4000] + "\nFINANCIAL TABLES\n" + "\n".join(relevant)[:5000]
        observations.append({"record_id": name, "available_at": published, "event_time": published,
                             "content": excerpt, "source": url})
    dataset = {"symbol": "INTC", "as_of": cutoff.isoformat(), "sessions": bars, "records": observations}
    (root / "dataset.json").write_text(json.dumps(dataset, indent=2) + "\n")
    files = [{"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
             for name in ("yahoo-history.csv", "q1_2024.html", "q2_2024.html", "dataset.json")]
    manifest = {"schema_version": "pilot_dataset.v1", "benchmark_ready": False,
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "files": files,
                "coverage": {"prices": ["2023-01-03", bars[-1]["closes_at"]], "sessions": 60,
                             "financial_documents": [r[0] for r in reports]},
                "limitations": ["not all historical filings; only Q1/Q2 2024 original releases",
                                 "Yahoo historical feed is a current download, not archived historical vintages",
                                 "no news corpus; no historical knowledge-contamination guarantee",
                                 "September 1 dividend credited before September 3 opening, first following trading session"],
                "sources": [r[2] for r in reports] + ["https://finance.yahoo.com/quote/INTC/history/"],
                "calendar": {"name": "XNAS", "library": "exchange_calendars", "version": xc.__version__}}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"manifest": str(root / "manifest.json"), "sessions": len(bars),
                      "last_close": bars[-1]["closes_at"], "benchmark_ready": False}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("artifacts/data/intc-2024-pilot"))
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    if args.download:
        download(args.root)
    build(args.root)
