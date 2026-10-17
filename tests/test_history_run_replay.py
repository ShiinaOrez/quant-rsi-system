"""A tool-using trading episode must replay prompts, tool results and accounting exactly."""
import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.domain.shared import DomainError
from scripts import run_local_benchmark as runner, replay_saved_run as verifier

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks/tasks/intc-2024-07-11-60d.toml"


class HistoryReplayTest(unittest.TestCase):
    def test_recorded_episode_reexecutes_tools_and_detects_changed_evidence(self):
        task = replace(load_definition(TASK), horizon_sessions=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"record_id": "bar-fixture", "event_time": task.as_of.isoformat(),
                 "available_at": task.as_of.isoformat(), "content": '{"sma20": 100, "sma60": 90}',
                 "source": "fixture"},
                {"record_id": "filing-fixture", "event_time": task.as_of.isoformat(),
                 "available_at": task.as_of.isoformat(), "content": "Assets 100", "source": "fixture"}]
            data = json.dumps({"symbol": task.symbol, "as_of": task.as_of.isoformat(), "records": records,
                               "sessions": [{"opens_at": "2024-07-12T09:30:00-04:00",
                                             "closes_at": "2024-07-12T16:00:00-04:00",
                                             "raw_open": "100", "raw_close": "101", "actions": []}]}).encode()
            (root / "dataset.json").write_bytes(data)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": "pilot_dataset.v1", "benchmark_ready": False,
                                             "files": [{"path": "dataset.json", "sha256": hashlib.sha256(data).hexdigest()}],
                                             "limitations": ["synthetic one-session integration fixture"]}))
            steps = iter([
                {"tool": "search_history", "arguments": {"kind": "filing", "query": ""}},
                {"tool": "read_history", "arguments": {"record_id": "filing-fixture"}},
                {"action": {"target_weight": 0.5, "rationale": "fixture evidence"}}])

            def sample(_policy, body):
                return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(next(steps))}}

            args = SimpleNamespace(pilot=True, task=TASK, manifest=manifest, model="qwen3:8b",
                                   endpoint="http://127.0.0.1:11434", output=root / "runs", run_id="fixture",
                                   history_tools=True, require_history_evidence=True)
            tags = json.dumps({"models": [{"name": args.model, "digest": "fixture-digest"}]}).encode()
            with patch.object(runner, "load_definition", return_value=task), \
                    patch.object(runner, "urlopen", return_value=io.BytesIO(tags)), \
                    patch.object(OllamaHistoryTrader, "_request", sample), contextlib.redirect_stdout(io.StringIO()):
                runner.run(args)
            run_dir = args.output / args.run_id
            result = json.loads((run_dir / "result.json").read_text())
            self.assertEqual(result["benchmark_status"], "diagnostic_pilot_incomplete_archive")
            self.assertTrue(result["history_policy"]["require_history_evidence"])
            with patch.object(verifier, "load_definition", return_value=task), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("replay must not use network")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                verifier.verify(run_dir, TASK, manifest)
            self.assertEqual(json.loads(output.getvalue())["model_calls"], 3)
            original_scan = FrozenPilotMarket.scan

            def changed_scan(market, symbol, clock):
                return tuple(replace(item, record=replace(item.record, content="modified evidence"))
                             if item.record.record_id == "filing-fixture" else item
                             for item in original_scan(market, symbol, clock))

            with patch.object(verifier, "load_definition", return_value=task), \
                    patch.object(FrozenPilotMarket, "scan", changed_scan):
                with self.assertRaisesRegex(DomainError, "prompt/tool result mismatch"):
                    verifier.verify(run_dir, TASK, manifest)


if __name__ == "__main__":
    unittest.main()
