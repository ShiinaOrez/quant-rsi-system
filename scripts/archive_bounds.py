"""Derive archive cutoffs from declarative task clocks and the exchange calendar."""
from datetime import datetime, timedelta
from pathlib import Path


def suite_bounds(suite: Path, symbol: str | None = None):
    import exchange_calendars as xc
    from scripts.archive_sources import load_targets

    tasks = [task for task in load_targets(suite) if symbol is None or task["symbol"] == symbol]
    if not tasks:
        raise ValueError("no matching tasks in suite")
    windows = []
    for task in tasks:
        cutoff = datetime.fromisoformat(task["as_of"])
        horizon = task["horizon_sessions"]
        if type(horizon) is not int or horizon <= 0:
            raise ValueError("positive session horizon required")
        calendar = xc.get_calendar("XNAS", start=(cutoff - timedelta(days=10)).date().isoformat(),
                                   end=(cutoff + timedelta(days=horizon * 3 + 30)).date().isoformat())
        day = cutoff.date().isoformat()
        if not calendar.is_session(day) or calendar.session_close(day).to_pydatetime() != cutoff:
            raise ValueError(f"{task['task_id']}: cutoff differs from exchange close")
        future = [str(session.date()) for session in calendar.sessions if str(session.date()) > day][:horizon]
        if len(future) != horizon:
            raise ValueError("calendar does not cover requested horizon")
        windows.append({"task_id": task["task_id"], "symbol": task["symbol"],
                        "as_of": task["as_of"], "first_session": future[0], "terminal_session": future[-1]})
    return {"through": max(window["terminal_session"] for window in windows), "tasks": windows,
            "calendar_library_version": xc.__version__}
