"""Provider-independent, finalized trace contracts."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json

from .shared import aware, require
from .training import TrajectoryReference
from .benchmark import DatasetSplit


class TraceKind(StrEnum):
    TRADER = "trader"
    RESEARCHER = "researcher"


def object_json(value: str) -> dict:
    # Reject NaN/Infinity, including values accepted by Python's default parser.
    def invalid_constant(text):
        raise ValueError(f"nonfinite JSON constant: {text}")
    parsed = json.loads(value, parse_constant=invalid_constant)
    require(type(parsed) is dict, "trace payload must be a JSON object")
    json.dumps(parsed, allow_nan=False)
    return parsed


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    occurred_at: datetime
    event_type: str
    payload_json: str

    def __post_init__(self):
        require(type(self.sequence) is int and self.sequence >= 0, "invalid event sequence")
        aware(self.occurred_at)
        require(bool(self.event_type.strip()), "event type required")
        object_json(self.payload_json)


@dataclass(frozen=True)
class Trajectory:
    reference: TrajectoryReference
    run_id: str
    kind: TraceKind
    status: str
    events: tuple[TraceEvent, ...]

    def __post_init__(self):
        require(bool(self.run_id.strip()), "run identity required")
        require(isinstance(self.kind, TraceKind), "typed trace kind required")
        require(self.status in ("completed", "failed"), "only finalized traces accepted")
        require(type(self.events) is tuple and bool(self.events), "nonempty immutable events required")
        require(tuple(e.sequence for e in self.events) == tuple(range(len(self.events))),
                "event sequences must be contiguous from zero")
        require(all(a.occurred_at <= b.occurred_at for a, b in zip(self.events, self.events[1:])),
                "event timestamps must be ordered")

    def training_reference(self) -> TrajectoryReference:
        require(self.kind == TraceKind.TRADER and self.status == "completed"
                and self.reference.split == DatasetSplit.TRAIN,
                "only completed trader training traces may be exported for training")
        return self.reference
