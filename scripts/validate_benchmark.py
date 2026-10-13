"""Validate the v0.1 TOML benchmark contract; no trading engine is invoked."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tomllib


class ConfigError(ValueError):
    pass


# Every field is required and unknown fields are rejected. Enum tuples freeze
# semantics while numeric values remain task-configurable.
ENV = {
    "schema_version": ("0.1",), "id": str,
    "market": {"asset_class": ("equity",), "exchange": ("NASDAQ",),
               "timezone": ("America/New_York",), "currency": ("USD",),
               "calendar": ("XNAS",)},
    "observation": {
        "frequency": ("daily",), "decision_time": ("after_close",),
        "visibility": ("available_at_lte_decision_time",),
        "financial_revision": ("as_published",), "history_access": ("query",),
        "required_data": list,
    },
    "indicators": {
        "price_basis": ("split_adjusted_as_of",), "sma_windows": list,
        "rsi_window": int, "rsi_method": ("wilder",), "volatility_window": int,
        "volatility_ddof": (1,), "warmup": ("null_until_ready",),
    },
    "action": {"type": ("target_weight",), "min_weight": float,
               "max_weight": float, "shorting": (False,), "leverage": (False,)},
    "execution": {
        "timing": ("next_session_open",), "price_field": ("raw_open",),
        "fractional_shares": (False,), "rounding": ("floor",),
        "commission_bps": float, "slippage_bps": float,
        "cash_interest_rate": (0.0,), "missing_open": ("reject_order",),
        "untradable": ("reject_order",), "dividends": ("credit_on_pay_date",),
        "splits": ("adjust_shares_on_effective_date",),
    },
    "valuation": {"price_field": ("raw_close",),
                  "missing_close": ("last_known_close",),
                  "terminal": ("mark_to_market",)},
    "evaluation": {"metrics": list, "baselines": list},
    "reproducibility": {"seed": int, "engine_contract": ("single_asset_daily_v1",)},
}
TASK = {
    "schema_version": ("0.1",), "id": str, "status": ("draft", "ready"),
    "environment": str, "split": ("train", "validation", "test"),
    "instrument": {"symbol": str},
    "episode": {"as_of": str, "start": ("next_session",),
                "horizon_sessions": int, "initial_cash": float,
                "initial_weight": (0.0,)},
    "dataset": {"manifest": str, "sha256": str},
    "goal": {"type": ("terminal_return_with_drawdown_limit",),
             "net_return_gte": float, "max_drawdown_lte": float},
}
DATA = {"ohlcv", "filings", "income_statement", "balance_sheet",
        "cash_flow_statement", "corporate_actions"}
METRICS = {"net_return", "max_drawdown", "excess_return_vs_buy_hold",
           "turnover", "trade_count", "goal_success"}
BASELINES = {"cash", "buy_and_hold", "sma_20_60"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def check_fields(value: object, spec: dict, path: str) -> None:
    require(type(value) is dict, f"{path}: expected table")
    require(set(value) == set(spec),
            f"{path}: missing={sorted(set(spec) - set(value))}, "
            f"unknown={sorted(set(value) - set(spec))}")
    for key, rule in spec.items():
        item, name = value[key], f"{path}.{key}"
        if isinstance(rule, dict):
            check_fields(item, rule, name)
        elif isinstance(rule, tuple):
            require(any(type(item) is type(x) and item == x for x in rule),
                    f"{name}: expected one of {rule}")
        elif rule is float:
            require(type(item) in (int, float) and float("-inf") < item < float("inf"),
                    f"{name}: expected finite number")
        else:
            require(type(item) is rule, f"{name}: expected {rule.__name__}")
            if rule is str:
                require(bool(item.strip()), f"{name}: cannot be empty")


def check_list(values: list, allowed: set, path: str) -> None:
    require(all(type(v) is str for v in values), f"{path}: expected string items")
    require(len(values) == len(set(values)), f"{path}: duplicate items")
    require(set(values) == allowed, f"{path}: expected {sorted(allowed)}")


def validate_environment(env: dict) -> None:
    check_fields(env, ENV, "environment")
    check_list(env["observation"]["required_data"], DATA, "required_data")
    check_list(env["evaluation"]["metrics"], METRICS, "metrics")
    check_list(env["evaluation"]["baselines"], BASELINES, "baselines")
    indicators = env["indicators"]
    require(indicators["sma_windows"] == [20, 60]
            and all(type(v) is int for v in indicators["sma_windows"]),
            "sma_windows: v0.1 baseline requires [20, 60]")
    for key in ("rsi_window", "volatility_window"):
        require(indicators[key] >= 2, f"{key}: must be >= 2")
    action = env["action"]
    require(0 <= action["min_weight"] < action["max_weight"] <= 1,
            "action: weights must satisfy 0 <= min < max <= 1")
    require(action["min_weight"] == 0 and action["max_weight"] == 1,
            "action: v0.1 cash/buy-hold baselines require bounds [0, 1]")
    for key in ("commission_bps", "slippage_bps"):
        require(0 <= env["execution"][key] < 10000, f"{key}: must be in [0, 10000)")
    require(env["reproducibility"]["seed"] >= 0, "seed: must be nonnegative")


def load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def validate_task(path: Path, require_ready: bool = False) -> dict:
    task = load(path)
    check_fields(task, TASK, "task")
    env_path = (path.parent / task["environment"]).resolve()
    env = load(env_path)
    validate_environment(env)
    episode = task["episode"]
    try:
        as_of = datetime.fromisoformat(episode["as_of"])
    except ValueError as error:
        raise ConfigError("as_of: expected ISO-8601 timestamp") from error
    require(as_of.tzinfo is not None, "as_of: explicit timezone offset required")
    require(episode["horizon_sessions"] > 0, "horizon_sessions: must be positive")
    require(episode["initial_cash"] > 0, "initial_cash: must be positive")
    require(task["goal"]["net_return_gte"] >= 0, "net_return_gte: must be nonnegative")
    require(0 <= task["goal"]["max_drawdown_lte"] <= 1,
            "max_drawdown_lte: must be in [0, 1]")
    if require_ready:
        require(task["status"] == "ready", "task is draft; execution requires ready")
    dataset = task["dataset"]
    if task["status"] == "ready":
        require(dataset["manifest"] != "UNBOUND", "ready task needs dataset manifest")
        digest = dataset["sha256"]
        require(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
                "dataset.sha256: expected 64 lowercase hex characters")
        manifest = path.parent / dataset["manifest"]
        require(manifest.is_file(), "dataset manifest not found")
        require(hashlib.sha256(manifest.read_bytes()).hexdigest() == digest,
                "dataset manifest SHA-256 mismatch")
    return {"task": task["id"], "environment": env["id"],
            "status": task["status"], "configuration_valid": True,
            "engine_implemented": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(validate_task(args.task, args.require_ready), indent=2))
    except (ConfigError, OSError, tomllib.TOMLDecodeError) as error:
        print(f"Invalid benchmark: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
