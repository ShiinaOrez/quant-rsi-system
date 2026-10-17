"""Execute local Qwen against frozen historical pilot data and persist traces."""
import argparse
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from urllib.request import urlopen

from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_trader import OllamaTrader
from quant_rsi.adapters.in_memory import ConstantWeightPolicy
from quant_rsi.adapters.trajectory_store import SQLiteTrajectoryStore, LocalArtifactStore
from quant_rsi.application.recording import ReplayRecorder, wire
from quant_rsi.application.replay import replay
from quant_rsi.application.trajectory_export import export_jsonl
from quant_rsi.domain.shared import require
from quant_rsi.domain.trading import TargetWeight, HoldPosition
from quant_rsi.domain.training import TrajectoryReference


class BuyAndHold:
    def __init__(self):
        self.first = True

    def decide(self, snapshot, account):
        if self.first:
            self.first = False
            return TargetWeight(Decimal(1))
        return HoldPosition()


class MovingAverage:
    def decide(self, snapshot, account):
        bar = next(r for r in snapshot.records if r.record_id.startswith("bar-"))
        data = json.loads(bar.content)
        return TargetWeight(Decimal(int(data["sma20"] > data["sma60"])))


def run(args):
    require(args.pilot, "incomplete financial archive: explicit --pilot required")
    task = load_definition(args.task)
    market = FrozenPilotMarket(task, args.manifest)
    policy = OllamaTrader(args.model, args.endpoint,
                          mandate={"horizon_sessions": task.horizon_sessions,
                                   "initial_cash": str(task.initial_cash),
                                   "goal": wire(task.goal), "execution": wire(task.execution),
                                   **({"engine_contract": task.engine_contract}
                                      if task.engine_contract == "single_asset_daily_v2" else {})})
    with urlopen(policy.base_url + "/api/tags", timeout=30) as response:
        tags = json.load(response)
    model = next((m for m in tags["models"] if m["name"] == args.model), None)
    require(model is not None, "local model not installed")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
    output = args.output / run_id
    output.mkdir(parents=True, exist_ok=False)
    repository = SQLiteTrajectoryStore(args.output / "trajectories.sqlite3")
    artifacts = LocalArtifactStore(args.output / "blobs")
    ref = TrajectoryReference(run_id + "-trader", task.task_id, task.split,
                              task.configuration_digest, market.data_digest,
                              "ollama:" + args.model + "@" + model["digest"])

    def progress(step, event):
        print(json.dumps({"step": step, "total": task.horizon_sessions,
                          "equity": str(event.equity), "shares": event.account.shares}), flush=True)

    recorder = ReplayRecorder(ref, run_id, artifacts, progress)
    policy.capture = recorder.sampled
    started = time.monotonic()
    try:
        report = replay(task, run_id, policy, market, market, observer=recorder)
        recorder.add("evaluation", report)
        trace = recorder.finalize("completed")
    except Exception as error:
        recorder.add("failure", {"error_type": type(error).__name__, "message": str(error)})
        trace = recorder.finalize("failed")
        repository.save(trace)
        (output / "trajectory.jsonl").write_bytes(export_jsonl(repository, ref.trajectory_id))
        raise
    repository.save(trace)
    (output / "trajectory.jsonl").write_bytes(export_jsonl(repository, ref.trajectory_id))
    elapsed = time.monotonic() - started
    baselines = {
        "cash": replay(task, run_id + "-cash", ConstantWeightPolicy(TargetWeight(Decimal(0))), market, market),
        "buy_and_hold": replay(task, run_id + "-buy-hold", BuyAndHold(), market, market),
        "sma20_60": replay(task, run_id + "-sma", MovingAverage(), market, market),
    }
    result = {"run_id": run_id, "benchmark_status": "diagnostic_pilot_incomplete_archive",
              "task_id": task.task_id, "configuration_digest": task.configuration_digest,
              "data_digest": market.data_digest, "policy_revision": ref.policy_revision,
              "model_details": model.get("details"), "report": wire(report),
              "baselines": wire(baselines), "limitations": market.manifest["limitations"],
              "elapsed_seconds": round(elapsed, 2), "trajectory_id": ref.trajectory_id,
              "rl_training_ready": False}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    artifacts.put((output / "result.json").read_bytes())
    print(json.dumps({"result": str(output / "result.json"), "report": wire(report),
                      "elapsed_seconds": result["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, default=Path("benchmarks/tasks/intc-2024-07-11-60d.toml"))
    parser.add_argument("--manifest", type=Path, default=Path("artifacts/data/intc-2024-pilot/manifest.json"))
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--output", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("--run-id")
    parser.add_argument("--pilot", action="store_true")
    run(parser.parse_args())
