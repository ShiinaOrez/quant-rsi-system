from dataclasses import asdict
from datetime import datetime
import hashlib
import json

from quant_rsi.domain.shared import DatasetSplit, require
from quant_rsi.domain.training import TrajectoryReference
from quant_rsi.domain.trajectory import TraceEvent, TraceKind, Trajectory, object_json

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def document(trace: Trajectory) -> dict:
    reference = asdict(trace.reference)
    reference["split"] = trace.reference.split.value
    return {"schema_version": "trajectory.v1", "reference": reference,
            "run_id": trace.run_id, "kind": trace.kind.value, "status": trace.status,
            "events": [{"sequence": e.sequence, "occurred_at": e.occurred_at.isoformat(),
                        "event_type": e.event_type, "payload": object_json(e.payload_json)}
                       for e in trace.events]}


def decode_document(raw: bytes) -> Trajectory:
    value = json.loads(raw)
    require(value["schema_version"] == "trajectory.v1", "unsupported trajectory schema")
    ref = value["reference"]
    ref["split"] = DatasetSplit(ref["split"])
    return Trajectory(TrajectoryReference(**ref), value["run_id"], TraceKind(value["kind"]),
                      value["status"], tuple(TraceEvent(
                          e["sequence"], datetime.fromisoformat(e["occurred_at"]),
                          e["event_type"], canonical(e["payload"]).decode()) for e in value["events"]))


def encode_jsonl(trace: Trajectory) -> bytes:
    value = document(trace)
    events = value.pop("events")
    lines = [canonical({"record_type": "header", **value})]
    lines.extend(canonical({"record_type": "event", **event}) for event in events)
    body = b"\n".join(lines) + b"\n"
    footer = canonical({"record_type": "footer", "event_count": len(events),
                        "body_sha256": hashlib.sha256(body).hexdigest()})
    return body + footer + b"\n"
