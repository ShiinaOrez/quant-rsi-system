import hashlib

from quant_rsi.domain.shared import require
from quant_rsi.domain.trajectory import Trajectory
from quant_rsi.serialization.trajectory import canonical, decode_document, document


class InMemoryTrajectoryRepository:
    def __init__(self):
        self._records: dict[str, bytes] = {}

    def save(self, trace: Trajectory) -> str:
        raw = canonical(document(trace))
        key = trace.reference.trajectory_id
        require(key not in self._records or self._records[key] == raw,
                "trajectory ID already contains different content")
        self._records[key] = raw
        return hashlib.sha256(raw).hexdigest()

    def get(self, trajectory_id: str) -> Trajectory:
        require(trajectory_id in self._records, "trajectory not found")
        return decode_document(self._records[trajectory_id])
