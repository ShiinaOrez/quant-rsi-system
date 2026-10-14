from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
import unittest

from quant_rsi.adapters.in_memory import ConstantWeightPolicy, ExplicitSessions, InMemoryHistory
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.application.replay import replay
from quant_rsi.domain.benchmark import DatasetSplit, ExecutionRules
from quant_rsi.domain.evaluation import evaluate
from quant_rsi.domain.market import InformationRecord, MarketSnapshot, TradingSession
from quant_rsi.domain.shared import DomainError
from quant_rsi.domain.trading import EpisodeStatus, TargetWeight, TradingEpisode
from quant_rsi.domain.training import PostTrainingPlan, TrainingMethod, TrajectoryReference

ROOT = Path(__file__).resolve().parents[1]


def instant(day, time="16:00:00"):
    return datetime.fromisoformat(f"2024-07-{day:02}T{time}-04:00")


def task(horizon=1, costs=False):
    config = load_definition(ROOT / "benchmarks/tasks/intc-2024-07-11-60d.toml")
    return replace(config, horizon_sessions=horizon, initial_cash=D(1000),
                   execution=ExecutionRules(D(5 if costs else 0), D(5 if costs else 0)))


def session(day=12, opening="100", closing="100", **kwargs):
    return TradingSession("INTC", instant(day, "09:30:00"), instant(day),
                          D(opening) if opening is not None else None,
                          D(closing) if closing is not None else None, **kwargs)


class DomainTests(unittest.TestCase):
    def test_toml_compiles_exact_costs_and_provenance(self):
        value = load_definition(ROOT / "benchmarks/tasks/intc-2024-07-11-60d.toml")
        self.assertEqual(value.goal.net_return_gte, D("0.05"))
        self.assertEqual(value.execution.commission_bps, D(5))
        self.assertEqual(value.split, DatasetSplit.VALIDATION)

    def test_full_weight_buy_respects_costs_and_cash(self):
        episode = TradingEpisode("one", task(costs=True))
        event = episode.settle_next_session(TargetWeight(D(1)), session())
        # Ten shares would overspend; buy nine after fees and slippage.
        self.assertEqual(event.account.shares, 9)
        self.assertEqual(event.account.cash, D("99.099775"))
        self.assertEqual(event.commission, D("0.450225"))
        self.assertEqual(episode.status, EpisodeStatus.COMPLETED)

    def test_sell_and_terminal_return_drawdown(self):
        episode = TradingEpisode("two", task(2))
        episode.settle_next_session(TargetWeight(D(1)), session(closing="90"))
        episode.settle_next_session(TargetWeight(D(0)), session(15, "110", "120"))
        report = evaluate(episode)
        self.assertEqual(episode.account.shares, 0)
        self.assertEqual(episode.account.cash, D(1100))
        self.assertEqual(report.net_return, D("0.1"))
        self.assertEqual(report.max_drawdown, D("0.1"))
        self.assertTrue(report.goal_success)
        self.assertEqual(report.trade_count, 2)

    def test_missing_open_rejects_without_deferred_execution(self):
        episode = TradingEpisode("reject", task(2))
        event = episode.settle_next_session(TargetWeight(D(1)), session(opening=None))
        self.assertEqual(event.rejection, "missing_open")
        episode.settle_next_session(TargetWeight(D(0)), session(15))
        self.assertEqual(episode.account.shares, 0)

    def test_missing_close_and_unsupported_action_are_atomic(self):
        episode = TradingEpisode("atomic", task(2))
        for bar in (session(closing=None), session(corporate_actions=("split",))):
            with self.assertRaises(DomainError):
                episode.settle_next_session(TargetWeight(D(1)), bar)
            self.assertEqual(episode.account.cash, D(1000))
            self.assertEqual(episode.events, ())
            self.assertEqual(episode.decision_time, instant(11))
        episode.settle_next_session(TargetWeight(D(1)), session())
        event = episode.settle_next_session(TargetWeight(D(1)), session(15, closing=None))
        self.assertTrue(event.stale_valuation)
        self.assertEqual(event.equity, D(1000))

    def test_clock_completion_and_partial_scoring_guards(self):
        episode = TradingEpisode("clock", task())
        with self.assertRaises(DomainError):
            evaluate(episode)
        with self.assertRaises(DomainError):
            episode.settle_next_session(TargetWeight(D(1)), session(11))
        episode.settle_next_session(TargetWeight(D(1)), session())
        with self.assertRaises(DomainError):
            episode.settle_next_session(TargetWeight(D(1)), session(15))

    def test_policy_only_receives_information_published_by_cutoff(self):
        initial = InformationRecord("old", "INTC", instant(10), instant(11), "old", "fixture")
        future = InformationRecord("new", "INTC", instant(1), instant(12), "new", "fixture")
        foreign = replace(initial, record_id="foreign", symbol="AAPL")
        seen = []

        class Policy:
            def decide(self, snapshot, account):
                seen.append(tuple(r.record_id for r in snapshot.records))
                return TargetWeight(D(0))

        report = replay(task(2), "replay", Policy(), InMemoryHistory((initial, future, foreign)),
                        ExplicitSessions((session(), session(15))))
        self.assertEqual(seen, [("old",), ("old", "new")])
        self.assertEqual(report.net_return, 0)
        with self.assertRaises(DomainError):
            MarketSnapshot("INTC", instant(11), (future,))

    def test_validation_and_test_are_forbidden_training_inputs(self):
        reference = TrajectoryReference("trace", "task", DatasetSplit.TRAIN,
                                        "a" * 64, "b" * 64, "base-v1")
        PostTrainingPlan("run", "model", TrainingMethod.SFT, "objective-v1", (reference,))
        for split in (DatasetSplit.VALIDATION, DatasetSplit.TEST):
            with self.assertRaises(DomainError):
                PostTrainingPlan("run", "model", TrainingMethod.RL, "reward-v1",
                                 (replace(reference, split=split),))


if __name__ == "__main__":
    unittest.main()
