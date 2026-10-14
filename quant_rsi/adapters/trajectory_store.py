"""Atomic immutable trajectory archive, JSONL export and local blob storage."""
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from quant_rsi.domain.benchmark import DatasetSplit
from quant_rsi.domain.shared import require
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


class SQLiteTrajectoryStore:
    """One committed envelope is authoritative; JSONL is a derived artifact.

    DELETE journal avoids requiring a particular patched WAL runtime. Database
    files belong on a local disk; connection transactions serialize writers.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS trajectories (
                trajectory_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, split TEXT NOT NULL, status TEXT NOT NULL,
                digest TEXT NOT NULL, document BLOB NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS run_split ON trajectories(run_id, split)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def save(self, trace: Trajectory) -> str:
        raw = canonical(document(trace))
        digest = hashlib.sha256(raw).hexdigest()
        # Same-ID retries are idempotent; changing recorded history is forbidden.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest FROM trajectories WHERE trajectory_id=?",
                             (trace.reference.trajectory_id,)).fetchone()
            if row:
                require(row[0] == digest, "trajectory ID already contains different content")
            else:
                db.execute("INSERT INTO trajectories VALUES(?,?,?,?,?,?,?)", (
                    trace.reference.trajectory_id, trace.run_id, trace.kind.value,
                    trace.reference.split.value, trace.status, digest, raw))
        return digest

    def get(self, trajectory_id: str) -> Trajectory:
        with self._connect() as db:
            row = db.execute("SELECT digest, document FROM trajectories WHERE trajectory_id=?",
                             (trajectory_id,)).fetchone()
        require(row is not None, "trajectory not found")
        digest, raw = row
        require(hashlib.sha256(raw).hexdigest() == digest, "trajectory integrity failure")
        value = json.loads(raw)
        require(value["schema_version"] == "trajectory.v1", "unsupported trajectory schema")
        ref = value["reference"]
        ref["split"] = DatasetSplit(ref["split"])
        return Trajectory(TrajectoryReference(**ref), value["run_id"], TraceKind(value["kind"]),
                          value["status"], tuple(TraceEvent(
                              e["sequence"], datetime.fromisoformat(e["occurred_at"]),
                              e["event_type"], canonical(e["payload"]).decode()) for e in value["events"]))

    def export_jsonl(self, trajectory_id: str) -> bytes:
        value = document(self.get(trajectory_id))
        events = value.pop("events")
        lines = [canonical({"record_type": "header", **value})]
        lines.extend(canonical({"record_type": "event", **event}) for event in events)
        # Integrity footer covers exact preceding bytes, including newlines.
        body = b"\n".join(lines) + b"\n"
        footer = canonical({"record_type": "footer", "event_count": len(events),
                            "body_sha256": hashlib.sha256(body).hexdigest()})
        return body + footer + b"\n"


class LocalArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest
        if target.exists():
            require(target.read_bytes() == content, "artifact integrity failure")
            return digest
        descriptor, temp = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return digest

    def get(self, digest: str) -> bytes:
        require(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
                "invalid artifact SHA-256")
        raw = (self.root / digest).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == digest, "artifact integrity failure")
        return raw
