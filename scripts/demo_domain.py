"""Run a synthetic domain demonstration; never a historical INTC backtest."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from quant_rsi.adapters.in_memory import ConstantWeightPolicy, ExplicitSessions, InMemoryHistory
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.application.replay import replay
from quant_rsi.domain.benchmark import ExecutionRules
from quant_rsi.domain.market import TradingSession
from quant_rsi.domain.trading import TargetWeight


def main():
    root = Path(__file__).resolve().parents[1]
    original = load_definition(root / "benchmarks/tasks/intc-2024-07-11-60d.toml")
    task = replace(original, task_id="synthetic-demo", horizon_sessions=2,
                   execution=ExecutionRules(Decimal(0), Decimal(0)))
    bars = tuple(TradingSession(
        "INTC", datetime.fromisoformat(f"2024-07-{day}T09:30:00-04:00"),
        datetime.fromisoformat(f"2024-07-{day}T16:00:00-04:00"),
        Decimal(opening), Decimal(closing))
        for day, opening, closing in [("12", "100", "90"), ("15", "90", "110")])
    report = replay(task, "demo", ConstantWeightPolicy(TargetWeight(Decimal(1))),
                    InMemoryHistory(()), ExplicitSessions(bars))
    print(json.dumps({"data": "synthetic_fixture_not_historical",
                      "net_return": str(report.net_return),
                      "max_drawdown": str(report.max_drawdown),
                      "goal_success": report.goal_success}, indent=2))


if __name__ == "__main__":
    main()
