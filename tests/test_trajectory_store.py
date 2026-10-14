from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from quant_rsi.adapters.trajectory_store import LocalArtifactStore, SQLiteTrajectoryStore
from quant_rsi.domain.benchmark import DatasetSplit
from quant_rsi.domain.shared import DomainError
from quant_rsi.domain.training import TrajectoryReference
from quant_rsi.domain.trajectory import TraceEvent, TraceKind, Trajectory


def trace():
    ref = TrajectoryReference("trace-1", "task-1", DatasetSplit.TRAIN, "a" * 64,
                              "b" * 64, "qwen-checkpoint-1")
    event = TraceEvent(0, datetime(2024, 7, 11, tzinfo=timezone.utc), "decision",
                       '{"target_weight":"0.5"}')
    return Trajectory(ref, "run-1", TraceKind.TRADER, "completed", (event,))


class TrajectoryStoreTests(unittest.TestCase):
    def test_roundtrip_restart_idempotency_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "traces.sqlite3"
            store = SQLiteTrajectoryStore(path)
            original = trace()
            digest = store.save(original)
            restarted = SQLiteTrajectoryStore(path)
            self.assertEqual(restarted.get("trace-1"), original)
            self.assertEqual(restarted.save(original), digest)
            with self.assertRaisesRegex(DomainError, "different content"):
                restarted.save(replace(original, status="failed"))
            self.assertEqual(restarted.get("trace-1"), original)

    def test_export_footer_covers_exact_body(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteTrajectoryStore(Path(folder) / "db")
            store.save(trace())
            lines = store.export_jsonl("trace-1").splitlines(keepends=True)
            footer = json.loads(lines[-1])
            self.assertEqual(footer["body_sha256"], hashlib.sha256(b"".join(lines[:-1])).hexdigest())
            self.assertEqual(footer["event_count"], 1)
            self.assertEqual(json.loads(lines[1])["payload"]["target_weight"], "0.5")

    def test_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "db"
            store = SQLiteTrajectoryStore(path)
            store.save(trace())
            with sqlite3.connect(path) as db:
                db.execute("UPDATE trajectories SET document=?", (b"{}",))
            with self.assertRaisesRegex(DomainError, "integrity"):
                store.get("trace-1")

    def test_invalid_sequences_and_training_sources_rejected(self):
        original = trace()
        with self.assertRaises(DomainError):
            replace(original, events=(replace(original.events[0], sequence=1),))
        self.assertEqual(original.training_reference(), original.reference)
        for bad in (replace(original, kind=TraceKind.RESEARCHER),
                    replace(original, status="failed"),
                    replace(original, reference=replace(original.reference, split=DatasetSplit.TEST))):
            with self.assertRaises(DomainError):
                bad.training_reference()
        for payload in ('{"value":NaN}', '{"value":1e999}'):
            with self.assertRaises(ValueError):
                replace(original.events[0], payload_json=payload)

    def test_blob_hash_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = LocalArtifactStore(root)
            digest = store.put(b"raw harness log")
            self.assertEqual(store.put(b"raw harness log"), digest)
            self.assertEqual(store.get(digest), b"raw harness log")
            (root / digest).write_bytes(b"changed")
            with self.assertRaises(DomainError):
                store.get(digest)
            with self.assertRaises(DomainError):
                store.get("../outside")
