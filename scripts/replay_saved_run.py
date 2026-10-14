"""Reproduce accounting exactly from immutable sampled actions, without inference."""
import argparse
import json
from pathlib import Path

from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_trader import parse_action
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.adapters.trajectory_store import SQLiteTrajectoryStore, LocalArtifactStore
from quant_rsi.application.recording import wire
from quant_rsi.application.replay import replay
from quant_rsi.domain.shared import require


def verify(run_dir: Path, task_path: Path, manifest_path: Path):
    result = json.loads((run_dir / "result.json").read_text())
    task = load_definition(task_path)
    market = FrozenPilotMarket(task, manifest_path)
    require(result["configuration_digest"] == task.configuration_digest and
            result["data_digest"] == market.data_digest, "replay provenance mismatch")
    trace = SQLiteTrajectoryStore(run_dir.parent / "trajectories.sqlite3").get(result["trajectory_id"])
    require(trace.status == "completed" and trace.reference.policy_revision == result["policy_revision"] and
            trace.reference.configuration_digest == task.configuration_digest and
            trace.reference.data_digest == market.data_digest,
            "completed trace with matching policy required")
    artifacts = LocalArtifactStore(run_dir.parent / "blobs")
    samples = [json.loads(artifacts.get(json.loads(e.payload_json)["artifact_sha256"]))
               for e in trace.events if e.event_type == "policy_sample"]
    require(len(samples) == task.horizon_sessions, "sample count mismatch")

    class RecordedPolicy:
        def __init__(self):
            self.index = 0

        def decide(self, snapshot, account):
            sample = samples[self.index]
            observation = json.loads(sample["request"]["messages"][-1]["content"])
            require(observation["decision_time"] == snapshot.decision_time.isoformat() and
                    observation["account"] == {"cash": str(account.cash), "shares": account.shares},
                    "recorded input/account mismatch")
            docs = json.loads(sample["request"]["messages"][1]["content"])["visible_documents"]
            expected = [{"record_id": r.record_id, "available_at": r.available_at.isoformat(),
                         "content": r.content} for r in snapshot.records]
            require(observation["records"] + docs == expected, "recorded visible information mismatch")
            self.index += 1
            return parse_action(sample["response"]["message"]["content"])

    report = replay(task, result["run_id"], RecordedPolicy(), market, market)
    require(wire(report) == result["report"], "accounting replay differs from recorded result")
    print(json.dumps({"verified": True, "decisions": len(samples), "report": wire(report)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--task", type=Path, default=Path("benchmarks/tasks/intc-2024-07-11-60d.toml"))
    parser.add_argument("--manifest", type=Path, default=Path("artifacts/data/intc-2024-pilot/manifest.json"))
    args = parser.parse_args()
    verify(args.run_dir, args.task, args.manifest)
