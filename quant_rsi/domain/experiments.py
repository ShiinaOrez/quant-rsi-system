"""RSI evaluation contracts: no stock, calendar or database semantics."""
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .shared import DatasetSplit, require


def digest(value: str) -> None:
    require(len(value) == 64 and all(c in "0123456789abcdef" for c in value), "SHA-256 required")


@dataclass(frozen=True)
class BenchmarkIdentity:
    suite_id: str
    version: str

    def __post_init__(self):
        require(bool(self.suite_id.strip()) and bool(self.version.strip()), "versioned suite required")


@dataclass(frozen=True)
class BenchmarkCase:
    benchmark: BenchmarkIdentity
    case_id: str
    split: DatasetSplit
    configuration_digest: str
    data_digest: str

    def __post_init__(self):
        require(bool(self.case_id.strip()), "case identity required")
        require(isinstance(self.split, DatasetSplit), "typed split required")
        digest(self.configuration_digest)
        digest(self.data_digest)


@dataclass(frozen=True)
class EvaluationRequest:
    evaluation_id: str
    run_id: str
    case: BenchmarkCase
    policy_revision: str

    def __post_init__(self):
        require(all(v.strip() for v in (self.evaluation_id, self.run_id, self.policy_revision)),
                "evaluation, run and policy identities required")


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class Metric:
    name: str
    value: Decimal
    unit: str
    direction: MetricDirection

    def __post_init__(self):
        require(bool(self.name.strip()) and bool(self.unit.strip()), "metric name/unit required")
        require(isinstance(self.value, Decimal) and self.value.is_finite(), "finite Decimal required")
        require(isinstance(self.direction, MetricDirection), "typed metric direction required")


@dataclass(frozen=True)
class BenchmarkOutcome:
    request: EvaluationRequest
    metrics: tuple[Metric, ...]
    success: bool | None
    trajectory_ids: tuple[str, ...] = ()

    def __post_init__(self):
        require(type(self.metrics) is tuple and bool(self.metrics), "immutable metrics required")
        require(len({m.name for m in self.metrics}) == len(self.metrics), "duplicate metrics")
        require(self.success is None or type(self.success) is bool, "success must be bool or unset")
        require(type(self.trajectory_ids) is tuple and all(self.trajectory_ids), "invalid trace references")
