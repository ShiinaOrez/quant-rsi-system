import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quant_rsi.domain.benchmark import BenchmarkTask
from quant_rsi.domain.corporate_actions import DividendEx, DividendPay
from quant_rsi.domain.market import InformationRecord, TradingSession, snapshot_at
from quant_rsi.domain.shared import require


class FrozenPilotMarket:
    """Verified local subset for diagnostic runs, not a complete archive adapter."""

    def __init__(self, task: BenchmarkTask, manifest_path: Path):
        root = manifest_path.parent.resolve()
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        require(manifest["schema_version"] == "pilot_dataset.v1", "unsupported pilot dataset")
        require(manifest["benchmark_ready"] is False, "pilot requires explicit incomplete-data status")
        entries = {f["path"]: f for f in manifest["files"]}
        require("dataset.json" in entries, "dataset file must be bound in manifest")
        for entry in entries.values():
            path = (root / entry["path"]).resolve()
            require(path.is_relative_to(root), "dataset path escapes bundle")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], "dataset integrity failure")
        value = json.loads((root / "dataset.json").read_text())
        require(value["symbol"] == task.symbol and datetime.fromisoformat(value["as_of"]) == task.as_of,
                "dataset instrument/cutoff mismatch")
        self.data_digest = hashlib.sha256(raw).hexdigest()
        self.manifest = manifest
        self._records = tuple(InformationRecord(
            r["record_id"], task.symbol, datetime.fromisoformat(r["event_time"]),
            datetime.fromisoformat(r["available_at"]), r["content"], r["source"]) for r in value["records"])
        bars = []
        for bar in value["sessions"]:
            actions = []
            for action in bar["actions"]:
                if action["type"] == "dividend_ex":
                    actions.append(DividendEx(action["id"], Decimal(action["amount_per_share"])))
                elif action["type"] == "dividend_pay":
                    actions.append(DividendPay(action["id"]))
                else:
                    require(False, "unsupported corporate action")
            bars.append(TradingSession(task.symbol, datetime.fromisoformat(bar["opens_at"]),
                                       datetime.fromisoformat(bar["closes_at"]), Decimal(bar["raw_open"]),
                                       Decimal(bar["raw_close"]), corporate_actions=tuple(actions)))
        self._sessions = tuple(bars)
        require(len(bars) == task.horizon_sessions, "dataset horizon mismatch")

    def snapshot(self, symbol, decision_time):
        visible = snapshot_at(symbol, decision_time, self._records)
        # Prompt selection is deterministic and contains only already visible records.
        bars = [r for r in visible.records if r.record_id.startswith("bar-")]
        reports = [r for r in visible.records if not r.record_id.startswith("bar-")]
        return type(visible)(symbol, decision_time, tuple(bars[-1:] + reports[-1:]))

    def next_sessions(self, task):
        require(task.symbol == self._sessions[0].symbol and task.horizon_sessions == len(self._sessions),
                "session request mismatch")
        return self._sessions
