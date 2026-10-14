from quant_rsi.domain.experiments import BenchmarkOutcome
from quant_rsi.domain.shared import require


class InMemoryEvaluationRepository:
    def __init__(self):
        self._results: dict[str, BenchmarkOutcome] = {}

    def save(self, outcome: BenchmarkOutcome) -> None:
        key = outcome.request.evaluation_id
        require(key not in self._results or self._results[key] == outcome, "evaluation ID conflict")
        self._results[key] = outcome

    def get(self, evaluation_id: str) -> BenchmarkOutcome:
        require(evaluation_id in self._results, "evaluation not found")
        return self._results[evaluation_id]
