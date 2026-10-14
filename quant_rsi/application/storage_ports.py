"""Storage contracts without SQL, filesystem or trading-engine dependencies."""
from typing import Protocol

from quant_rsi.domain.experiments import BenchmarkOutcome
from quant_rsi.domain.trajectory import Trajectory


class TrajectoryRepository(Protocol):
    def save(self, trace: Trajectory) -> str:
        """Atomic immutable ID; identical retries return the same content hash."""
        ...
    def get(self, trajectory_id: str) -> Trajectory: ...


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> str:
        """Return verified SHA-256; same content is idempotent across backends."""
        ...
    def get(self, digest: str) -> bytes:
        """Return content only after checking its digest."""
        ...


class EvaluationRepository(Protocol):
    def save(self, outcome: BenchmarkOutcome) -> None:
        """Immutable evaluation ID; identical retry allowed, conflicting content rejected."""
        ...
    def get(self, evaluation_id: str) -> BenchmarkOutcome: ...
