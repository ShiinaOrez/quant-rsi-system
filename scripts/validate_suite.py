"""Check a diagnostic suite's task links, fixed grid and private review records.

This checks definitions, not data completeness or strategy performance. Calendar
checks are optional and require the project's pinned market dependencies.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.validate_benchmark import ConfigError, load, require, validate_task


def linked(parent: Path, value: str, boundary: Path) -> Path:
    require(isinstance(value, str) and bool(value), "link must be a nonempty path")
    result = (parent / value).resolve()
    require(result.is_relative_to(boundary), "link escapes benchmark directory")
    require(result.is_file(), f"missing file: {result}")
    return result


def validate_suite(path: Path, with_calendar: bool = False) -> dict:
    path = path.resolve()
    boundary = path.parent.parent
    suite = load(path)
    require(suite["schema_version"] == "0.1", "unsupported suite version")
    require(suite["agent_visible"] is False, "suite contains private diagnostics")
    require(suite["selection"]["policy"] == "fixed_issuer_time_grid",
            "unexpected selection policy")
    require(suite["selection"]["prospective"] is False,
            "retrospective suite cannot claim prospective selection")
    symbols, cutoffs = suite["grid"]["symbols"], suite["grid"]["cutoffs"]
    require(len(symbols) == len(set(symbols)) and len(cutoffs) == len(set(cutoffs)),
            "grid contains duplicate axes")
    expected = {(symbol, cutoff) for symbol in symbols for cutoff in cutoffs}
    observed, ids, paths, windows = set(), set(), set(), []
    calendar = None
    if with_calendar:
        import exchange_calendars
        calendar = exchange_calendars.get_calendar("XNAS")

    def register(task_path: Path) -> dict:
        validate_task(task_path)
        task = load(task_path)
        require(task["id"] not in ids, "duplicate task id")
        require(task_path not in paths, "duplicate task path")
        ids.add(task["id"])
        paths.add(task_path)
        return task

    for legacy in suite["legacy_tasks"]:
        register(linked(path.parent, legacy, boundary))
    for case in suite["cases"]:
        task_path = linked(path.parent, case["task"], boundary)
        task = register(task_path)
        task_id = task["id"]
        cutoff = datetime.fromisoformat(task["episode"]["as_of"])
        local = cutoff.astimezone(ZoneInfo("America/New_York"))
        require(cutoff.utcoffset() == local.utcoffset() and cutoff.hour == 16,
                f"{task_id}: cutoff must use New York's closing-time offset")
        require(local.time() == time(16), f"{task_id}: cutoff must be 16:00")
        pair = task["instrument"]["symbol"], local.date().isoformat()
        require(pair not in observed, "duplicate grid cell")
        observed.add(pair)
        require(task["split"] == "validation", "new cases must be validation")
        require(task["environment"] == suite["environment"], "environment differs")
        require(task["goal"] == suite["goal"], "goal differs from fixed suite goal")
        require(task["episode"]["horizon_sessions"] == 60
                and task["episode"]["initial_cash"] == 100000,
                "new cases must preserve episode constraints")
        require(task_id == f"{pair[0].lower()}-{pair[1]}-60d", "id differs from grid")

        assessor_path = linked(path.parent, case["assessor"], boundary)
        plan_path = linked(path.parent, case["data_plan"], boundary)
        assessor, plan = load(assessor_path), load(plan_path)
        require(assessor["task_id"] == plan["task_id"] == task_id,
                "task/assessor/data-plan identity mismatch")
        require(assessor["agent_visible"] is False and plan["agent_visible"] is False,
                "review and data construction notes must remain private")
        require(assessor["selection"] == "fixed_issuer_time_grid", "selection differs")
        require(linked(assessor_path.parent, assessor["data_plan"], boundary) == plan_path,
                "assessor points at another data plan")
        require(plan["plan_version"] == "0.2", "unsupported data plan")
        require(plan["identity"]["symbol"] == pair[0], "data-plan symbol differs")
        require(plan["construction"]["initial_cutoff"] == cutoff.isoformat(),
                "data-plan cutoff differs")
        require(plan["construction"]["forward_sessions"] == 60, "data-plan horizon differs")
        template_path = linked(plan_path.parent, plan["template"], boundary)
        template = load(template_path)
        require(template["plan_version"] == "0.2", "unsupported shared data plan")
        require(template["availability"]["future_records"] ==
                "environment_private_until_available_at", "future records exposed")
        require(template["availability"]["missing_publication_time"] ==
                "next_session_open_after_publication_date", "publication fallback differs")
        require(bool(assessor["hypothesis"]["question"].strip()), "missing diagnostic question")
        require(bool(assessor["evidence"]), "missing initial evidence")
        for evidence in assessor["evidence"]:
            require(evidence["phase"] == "before_start", "unexpected future evidence")
            require(evidence["url"].startswith("https://"), "source must use HTTPS")
            # Date-only timestamps are conservatively usable on a later session.
            # Same-day releases need a verified intraday policy before acceptance.
            require(date.fromisoformat(evidence["publication_date"]) < local.date(),
                    "initial evidence is not safely before cutoff")
        seed = plan["seed"]
        require(any(e["url"] == seed["url"] and
                    e["publication_date"] == seed["publication_date"]
                    for e in assessor["evidence"]), "seed differs from reviewed evidence")
        if calendar is not None:
            require(calendar.is_session(pair[1]), f"{task_id}: cutoff is not a session")
            require(calendar.session_close(pair[1]).to_pydatetime() == cutoff,
                    f"{task_id}: calendar close differs")
            index = calendar.sessions.get_loc(pair[1])
            sessions = calendar.sessions[index + 1:index + 61]
            require(len(sessions) == 60, "calendar does not cover horizon")
            require(str(sessions[0].date()) == case["first_session"] and
                    str(sessions[-1].date()) == case["terminal_session"],
                    f"{task_id}: first/terminal session differs")
            windows.append({"id": task_id, "first": case["first_session"],
                            "terminal": case["terminal_session"]})
    require(observed == expected, "cases do not cover the declared issuer/time grid")
    require(len(ids) == suite["expected_task_count"], "suite task count differs")
    return {"id": suite["id"], "tasks": len(ids), "new_cases": len(observed),
            "calendar_checked": with_calendar, "data_materialized": False,
            "windows": windows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--with-calendar", action="store_true")
    args = parser.parse_args()
    try:
        result = validate_suite(args.suite, args.with_calendar)
    except (ConfigError, KeyError, ValueError, ImportError) as error:
        parser.exit(1, f"INVALID: {error}\n")
    print(f"VALID: {result['tasks']} task definitions; {result['new_cases']} new grid cases; "
          f"calendar_checked={result['calendar_checked']}; data_materialized=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
