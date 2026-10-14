from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json

from quant_rsi.domain.trajectory import TraceEvent, TraceKind, Trajectory
from quant_rsi.domain.training import TrajectoryReference


def wire(value):
    if is_dataclass(value):
        return wire(asdict(value))
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


class ReplayRecorder:
    def __init__(self, reference: TrajectoryReference, run_id: str, artifacts, progress=None):
        self.reference, self.run_id, self.artifacts = reference, run_id, artifacts
        self.events, self.progress = [], progress
        self.steps = 0

    def add(self, event_type, payload):
        self.events.append(TraceEvent(len(self.events), datetime.now(timezone.utc), event_type,
                                      json.dumps(wire(payload), allow_nan=False)))

    def sampled(self, sample):
        raw = json.dumps(sample, ensure_ascii=False, allow_nan=False).encode()
        self.add("policy_sample", {"artifact_sha256": self.artifacts.put(raw),
                                    "policy_revision": self.reference.policy_revision,
                                    "on_policy_training_ready": False,
                                    "prompt_eval_count": sample["response"].get("prompt_eval_count"),
                                    "eval_count": sample["response"].get("eval_count")})

    def settled(self, snapshot, before, target, event):
        self.add("decision", {"decision_time": snapshot.decision_time,
                              "visible_record_ids": [r.record_id for r in snapshot.records],
                              "account_before": before, "order_intent": target})
        self.add("session_settled", event)
        self.steps += 1
        if self.progress:
            self.progress(self.steps, event)

    def finalize(self, status):
        return Trajectory(self.reference, self.run_id, TraceKind.TRADER, status, tuple(self.events))
