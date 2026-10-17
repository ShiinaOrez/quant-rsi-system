import json
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
from quant_rsi.application.history import HistoryItem
from quant_rsi.domain.market import InformationRecord, MarketSnapshot
from quant_rsi.domain.trading import Account


NOW = datetime(2024, 7, 1, tzinfo=timezone.utc)


class Archive:
    def scan(self, symbol, available_through):
        # Deliberately unsafe provider: the service must filter these records.
        for identity, symbol, when, content in (
                ("past", "INTC", NOW, "Assets 100"),
                ("future", "INTC", NOW + timedelta(days=1), "future-secret"),
                ("other", "AAPL", NOW, "other-secret")):
            yield HistoryItem(InformationRecord(identity, symbol, when, when, content, "fixture"), "filing")


class FakeTrader(OllamaHistoryTrader):
    def __init__(self, steps, **kwargs):
        super().__init__(Archive(), **kwargs)
        self.steps, self.requests = iter(steps), []

    def _request(self, body):
        self.requests.append(body)
        return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(next(self.steps))}}


class HistoryDecisionTest(unittest.TestCase):
    snapshot = MarketSnapshot("INTC", NOW, ())
    account = Account(Decimal(1000), 0)
    action = {"action": {"target_weight": 0.25, "rationale": "evidence checked"}}

    def test_search_read_action_and_trace(self):
        events = []
        trader = FakeTrader([
            {"tool": "search_history", "arguments": {}},
            {"tool": "read_history", "arguments": {"record_id": "past"}}, self.action],
            capture=events.append, tool_capture=events.append, require_history_evidence=True)
        result = trader.decide(self.snapshot, self.account)
        self.assertEqual(result.value, Decimal("0.25"))
        self.assertEqual(trader.decisions, 1)
        serialized = json.dumps(trader.requests)
        self.assertIn("Assets 100", serialized)
        self.assertNotIn("future-secret", serialized)
        self.assertNotIn("other-secret", serialized)
        self.assertEqual(sum("request" in e for e in events), 3)
        self.assertEqual(sum("tool" in e for e in events), 2)

    def test_future_id_and_missing_id_have_same_response(self):
        replies = []
        for identity in ("future", "missing"):
            trader = FakeTrader([{"tool": "read_history", "arguments": {"record_id": identity}}, self.action])
            trader.decide(self.snapshot, self.account)
            replies.append(trader.requests[-1]["messages"][-1]["content"])
        self.assertEqual(*replies)
        self.assertIn("record not available", replies[0])

    def test_budget_failure_does_not_become_hold(self):
        trader = FakeTrader([{"tool": "search_history", "arguments": {}}] * 2, max_tool_calls=1)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            trader.decide(self.snapshot, self.account)
        self.assertEqual(trader.decisions, 0)

    def test_model_cannot_override_clock(self):
        trader = FakeTrader([{"tool": "search_history", "arguments": {"available_through": "2099"}}])
        from jsonschema import ValidationError
        with self.assertRaises(ValidationError):
            trader.decide(self.snapshot, self.account)

    def test_required_evidence_rejects_premature_action(self):
        from jsonschema import ValidationError
        trader = FakeTrader([self.action], require_history_evidence=True)
        with self.assertRaises(ValidationError):
            trader.decide(self.snapshot, self.account)
        self.assertEqual(trader.decisions, 0)

    def test_large_page_is_refused_and_smaller_page_can_be_read(self):
        class BigArchive:
            def scan(self, symbol, available_through):
                return (HistoryItem(InformationRecord("past", "INTC", NOW, NOW,
                                                     "evidence " * 2000, "fixture"), "filing"),)
        trader = FakeTrader([
            {"tool": "search_history", "arguments": {}},
            {"tool": "read_history", "arguments": {"record_id": "past", "limit": 8000}},
            {"tool": "read_history", "arguments": {"record_id": "past", "limit": 50}},
            self.action], require_history_evidence=True, max_message_bytes=8000)
        trader.archive = BigArchive()
        trader.decide(self.snapshot, self.account)
        refused = trader.requests[2]["messages"][-1]["content"]
        self.assertIn("page exceeds remaining context budget", refused)
        self.assertNotIn("evidence evidence", refused)
        # The refused page did not enable the action branch.
        self.assertEqual(len(trader.requests[2]["format"]["oneOf"]), 2)
        self.assertEqual(len(trader.requests[3]["format"]["oneOf"]), 3)


if __name__ == "__main__":
    unittest.main()
