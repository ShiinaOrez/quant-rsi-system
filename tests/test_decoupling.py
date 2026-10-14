from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from quant_rsi.adapters.in_memory import ConstantWeightPolicy, ExplicitSessions, InMemoryHistory
from quant_rsi.adapters.memory_evaluations import InMemoryEvaluationRepository
from quant_rsi.adapters.memory_trajectories import InMemoryTrajectoryRepository
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.adapters.trading_benchmark import ReplayCaseBinding, TradingReplayBenchmark
from quant_rsi.adapters.trajectory_store import LocalArtifactStore, SQLiteTrajectoryStore
from quant_rsi.application.experiments import EvaluateCandidate
from quant_rsi.application.trajectory_export import archive_export, export_jsonl
from quant_rsi.domain.experiments import (
    BenchmarkCase, BenchmarkIdentity, BenchmarkOutcome, EvaluationRequest, Metric, MetricDirection,
)
from quant_rsi.domain.market import TradingSession
from quant_rsi.domain.shared import DatasetSplit, DomainError
from quant_rsi.domain.trading import TargetWeight
from quant_rsi.domain.training import TrajectoryReference
from quant_rsi.domain.trajectory import TraceEvent, TraceKind, Trajectory

ROOT = Path(__file__).resolve().parents[1]


class DecouplingTests(unittest.TestCase):
    def test_same_repository_contract_and_export_for_memory_and_sqlite(self):
        ref = TrajectoryReference("trace", "suite/version/case", DatasetSplit.TRAIN,
                                  "a" * 64, "b" * 64, "policy")
        trace = Trajectory(ref, "run", TraceKind.TRADER, "completed", (
            TraceEvent(0, datetime(2024, 1, 1, tzinfo=timezone.utc), "decision", '{}'),))
        with tempfile.TemporaryDirectory() as directory:
            stores = [InMemoryTrajectoryRepository(), SQLiteTrajectoryStore(Path(directory) / "db")]
            artifacts = LocalArtifactStore(Path(directory) / "blobs")
            exports = []
            for repository in stores:
                first = repository.save(trace)
                self.assertEqual(repository.save(trace), first)
                self.assertEqual(repository.get("trace"), trace)
                with self.assertRaises(DomainError):
                    repository.save(replace(trace, status="failed"))
                exports.append(export_jsonl(repository, "trace"))
                blob = archive_export(repository, artifacts, "trace")
                self.assertEqual(artifacts.get(blob), exports[-1])
                with self.assertRaises(DomainError):
                    repository.get("absent")
            self.assertEqual(exports[0], exports[1])

    def test_one_rsi_use_case_runs_trading_and_nontrading_suite(self):
        task = replace(load_definition(ROOT / "benchmarks/tasks/intc-2024-07-11-60d.toml"),
                       horizon_sessions=1)
        case = BenchmarkCase(TradingReplayBenchmark.identity, task.task_id, task.split,
                             task.configuration_digest, "b" * 64)
        bar = TradingSession("INTC", datetime.fromisoformat("2024-07-12T09:30:00-04:00"),
                             datetime.fromisoformat("2024-07-12T16:00:00-04:00"), D(100), D(100))
        runner = TradingReplayBenchmark((ReplayCaseBinding(
            case, task, InMemoryHistory(()), ExplicitSessions((bar,))),),
            lambda revision: ConstantWeightPolicy(TargetWeight(D(0))))

        class ExternalSuiteFixture:
            # Contract demonstration only, not a real Finance Agents integration.
            identity = BenchmarkIdentity("agent_reasoning_fixture", "2")

            def evaluate(self, request):
                return BenchmarkOutcome(request, (
                    Metric("answer_accuracy", D("0.8"), "ratio", MetricDirection.MAXIMIZE),), None)

        external = ExternalSuiteFixture()
        external_case = BenchmarkCase(external.identity, "case1", DatasetSplit.VALIDATION,
                                      "c" * 64, "d" * 64)
        repository = InMemoryEvaluationRepository()
        service = EvaluateCandidate((runner, external), repository)
        trading = service.execute(EvaluationRequest("eval1", "rsi1", case, "policy1"))
        other = service.execute(EvaluationRequest("eval2", "rsi1", external_case, "policy1"))
        self.assertEqual(trading.metrics[0].name, "net_return")
        self.assertEqual(other.metrics[0].name, "answer_accuracy")
        self.assertIsNone(other.success)
        self.assertEqual(repository.get("eval2"), other)
        with self.assertRaises(DomainError):
            service.execute(EvaluationRequest("test", "rsi1", replace(external_case, split=DatasetSplit.TEST), "policy1"))
        with self.assertRaises(DomainError):
            service.execute(EvaluationRequest("unknown", "rsi1", replace(
                external_case, benchmark=BenchmarkIdentity("agent_reasoning_fixture", "3")), "policy1"))

    def test_wrong_provenance_is_not_persisted(self):
        class BadRunner:
            identity = BenchmarkIdentity("bad", "1")

            def evaluate(self, request):
                return BenchmarkOutcome(replace(request, policy_revision="wrong"), (
                    Metric("score", D(-1), "points", MetricDirection.MINIMIZE),), False)

        case = BenchmarkCase(BadRunner.identity, "case", DatasetSplit.VALIDATION,
                             "a" * 64, "b" * 64)
        repository = InMemoryEvaluationRepository()
        with self.assertRaisesRegex(DomainError, "provenance"):
            EvaluateCandidate((BadRunner(),), repository).execute(
                EvaluationRequest("eval", "run", case, "policy"))
        with self.assertRaises(DomainError):
            repository.get("eval")
