"""Reproduce accounting exactly from immutable sampled actions, without inference."""
import argparse
import json
from pathlib import Path

from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_trader import parse_action
from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
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
    history_config = result.get("history_policy")
    if not history_config:
        require(len(samples) == task.horizon_sessions, "sample count mismatch")

    class RecordedPolicy:
        def __init__(self):
            self.index = 0

        def decide(self, snapshot, account):
            sample = samples[self.index]
            observation = json.loads(sample["request"]["messages"][-1]["content"])
            expected_account = {"cash": str(account.cash), "shares": account.shares}
            if task.engine_contract == "single_asset_daily_v2":
                expected_account["dividend_receivable"] = str(account.dividend_receivable)
            require(observation["decision_time"] == snapshot.decision_time.isoformat() and
                    observation["account"] == expected_account,
                    "recorded input/account mismatch")
            docs = json.loads(sample["request"]["messages"][1]["content"])["visible_documents"]
            expected = [{"record_id": r.record_id, "available_at": r.available_at.isoformat(),
                         "content": r.content} for r in snapshot.records]
            require(observation["records"] + docs == expected, "recorded visible information mismatch")
            self.index += 1
            return parse_action(sample["response"]["message"]["content"])

    if history_config:
        require(bool(samples), "recorded model calls required")
        require(history_config["protocol_revision"] == OllamaHistoryTrader.protocol_revision,
                "unsupported history protocol")
        observation = json.loads(samples[0]["request"]["messages"][1]["content"])
        reproduced_tools = []

        class RecordedHistoryPolicy(OllamaHistoryTrader):
            def __init__(self):
                super().__init__(market, model=samples[0]["request"]["model"],
                                 seed=samples[0]["request"]["options"]["seed"],
                                 mandate=observation["mandate"],
                                 max_tool_calls=history_config["max_tool_calls"],
                                 max_message_bytes=history_config["max_message_bytes"],
                                 require_history_evidence=history_config["require_history_evidence"],
                                 tool_capture=reproduced_tools.append)
                self.sample_index = 0

            def _request(self, body):
                require(self.sample_index < len(samples), "recorded model call missing")
                sample = samples[self.sample_index]
                require(sample.get("protocol_revision") == self.protocol_revision and
                        sample.get("decision_index") == self.decisions and sample["request"] == body,
                        "recorded history prompt/tool result mismatch")
                self.sample_index += 1
                return sample["response"]

        policy = RecordedHistoryPolicy()
        report = replay(task, result["run_id"], policy, market, market)
        require(policy.sample_index == len(samples), "unused recorded model calls")
        recorded_tools = [json.loads(e.payload_json) for e in trace.events if e.event_type == "history_tool"]
        require(reproduced_tools == recorded_tools, "recorded history tool transcript mismatch")
    else:
        report = replay(task, result["run_id"], RecordedPolicy(), market, market)
    require(wire(report) == result["report"], "accounting replay differs from recorded result")
    print(json.dumps({"verified": True, "decisions": task.horizon_sessions,
                      "model_calls": len(samples), "report": wire(report)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--task", type=Path, default=Path("benchmarks/tasks/intc-2024-07-11-60d.toml"))
    parser.add_argument("--manifest", type=Path, default=Path("artifacts/data/intc-2024-pilot/manifest.json"))
    args = parser.parse_args()
    verify(args.run_dir, args.task, args.manifest)
