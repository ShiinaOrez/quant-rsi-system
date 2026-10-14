"""Ports used by RSI; finance-specific replay ports remain separate."""
from typing import Protocol

from quant_rsi.domain.experiments import BenchmarkIdentity, BenchmarkOutcome, EvaluationRequest
from .storage_ports import ArtifactStore, EvaluationRepository  # compatibility re-exports


class BenchmarkRunner(Protocol):
    @property
    def identity(self) -> BenchmarkIdentity: ...
    def evaluate(self, request: EvaluationRequest) -> BenchmarkOutcome: ...
