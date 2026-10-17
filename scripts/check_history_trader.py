"""Check local history-tool inference on the existing incomplete pilot, without scoring."""
import argparse
import json
from pathlib import Path
import time

from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.application.recording import wire
from quant_rsi.domain.trading import Account


def run(args):
    if args.output.exists():
        raise FileExistsError("diagnostic output already exists")
    task = load_definition(args.task)
    market = FrozenPilotMarket(task, args.manifest)
    events = []
    policy = OllamaHistoryTrader(market, model=args.model, base_url=args.endpoint,
                                capture=events.append, tool_capture=events.append,
                                require_history_evidence=args.require_evidence,
                                mandate={"horizon_sessions": task.horizon_sessions,
                                         "engine_contract": task.engine_contract})
    started = time.monotonic()
    result = {"status": "diagnostic_history_tool_protocol_only", "benchmark_ready": False,
              "protocol_revision": policy.protocol_revision,
              "require_history_evidence": args.require_evidence,
              "configuration_digest": task.configuration_digest, "data_digest": market.data_digest}
    try:
        result["action"] = wire(policy.decide(market.snapshot(task.symbol, task.as_of),
                                              Account(task.initial_cash, 0)))
    except Exception as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    result.update(elapsed_seconds=round(time.monotonic() - started, 2),
                  model_calls=sum("request" in event for event in events),
                  tool_calls=[event["tool"] for event in events if "tool" in event], events=events)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Preserve previous diagnostic results instead of overwriting them.
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "events"}))
    return int("error" in result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, default=Path("benchmarks/tasks/intc-2024-07-11-60d.toml"))
    parser.add_argument("--manifest", type=Path, default=Path("artifacts/data/intc-2024-pilot/manifest.json"))
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--require-evidence", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args()))
