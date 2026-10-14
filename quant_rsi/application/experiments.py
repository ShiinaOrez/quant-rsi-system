from collections.abc import Iterable

from quant_rsi.domain.experiments import BenchmarkOutcome, EvaluationRequest
from quant_rsi.domain.shared import DatasetSplit, require

from .experiment_ports import BenchmarkRunner
from .storage_ports import EvaluationRepository


class EvaluateCandidate:
    """RSI evaluation use case; bindings are injected at the composition root."""

    def __init__(self, runners: Iterable[BenchmarkRunner], repository: EvaluationRepository):
        self._runners = {}
        for runner in runners:
            require(runner.identity not in self._runners, "duplicate benchmark registration")
            self._runners[runner.identity] = runner
        self._repository = repository

    def execute(self, request: EvaluationRequest) -> BenchmarkOutcome:
        # A sealed-test run belongs to a separate final-evaluation use case.
        require(request.case.split != DatasetSplit.TEST, "RSI cannot request sealed test feedback")
        require(request.case.benchmark in self._runners, "benchmark/version not registered")
        outcome = self._runners[request.case.benchmark].evaluate(request)
        require(outcome.request == request, "benchmark returned mismatched evaluation provenance")
        self._repository.save(outcome)
        return outcome
