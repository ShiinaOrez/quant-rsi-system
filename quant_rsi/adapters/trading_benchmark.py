"""Anti-corruption adapter from single-stock replay to generic RSI contracts."""
from dataclasses import dataclass
from typing import Callable

from quant_rsi.application.ports import MarketHistory, SessionCalendar, TraderPolicy
from quant_rsi.application.replay import replay
from quant_rsi.domain.benchmark import BenchmarkTask
from quant_rsi.domain.experiments import (
    BenchmarkCase, BenchmarkIdentity, BenchmarkOutcome, EvaluationRequest, Metric, MetricDirection,
)
from quant_rsi.domain.shared import require


@dataclass(frozen=True)
class ReplayCaseBinding:
    case: BenchmarkCase
    task: BenchmarkTask
    history: MarketHistory
    calendar: SessionCalendar

    def __post_init__(self):
        require(self.case.case_id == self.task.task_id and self.case.split == self.task.split
                and self.case.configuration_digest == self.task.configuration_digest,
                "replay case and task provenance must agree")


class TradingReplayBenchmark:
    identity = BenchmarkIdentity("single_asset_daily", "0.1")

    def __init__(self, bindings: tuple[ReplayCaseBinding, ...],
                 resolve_policy: Callable[[str], TraderPolicy]):
        require(all(b.case.benchmark == self.identity for b in bindings), "wrong suite binding")
        require(len({b.case.case_id for b in bindings}) == len(bindings), "duplicate replay cases")
        self._bindings = {b.case.case_id: b for b in bindings}
        self._resolve_policy = resolve_policy

    def evaluate(self, request: EvaluationRequest) -> BenchmarkOutcome:
        require(request.case.benchmark == self.identity, "unsupported suite/version")
        require(request.case.case_id in self._bindings, "unknown replay case")
        binding = self._bindings[request.case.case_id]
        require(binding.case == request.case, "replay configuration/data/split mismatch")
        report = replay(binding.task, request.evaluation_id,
                        self._resolve_policy(request.policy_revision), binding.history, binding.calendar)
        metrics = (
            Metric("net_return", report.net_return, "ratio", MetricDirection.MAXIMIZE),
            Metric("max_drawdown", report.max_drawdown, "ratio", MetricDirection.MINIMIZE),
            Metric("turnover", report.turnover, "ratio", MetricDirection.DIAGNOSTIC),
        )
        # No persisted trajectory yet; never invent a trajectory reference.
        return BenchmarkOutcome(request, metrics, report.goal_success)
