from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_domain import task, session
from quant_rsi.adapters.frozen_pilot import FrozenPilotMarket
from quant_rsi.adapters.ollama_trader import OllamaTrader, parse_action
from quant_rsi.domain.corporate_actions import DividendEx, DividendPay
from quant_rsi.domain.market import MarketSnapshot
from quant_rsi.domain.shared import DomainError
from quant_rsi.domain.trading import TradingEpisode, TargetWeight, HoldPosition, Account


class LocalTraderTests(unittest.TestCase):
    def test_dividend_entitlement_survives_sale(self):
        episode = TradingEpisode("dividend", task(3))
        episode.settle_next_session(TargetWeight(D(1)), session())
        episode.settle_next_session(TargetWeight(D(0)), session(15, corporate_actions=(DividendEx("d", D("0.125")),)))
        event = episode.settle_next_session(HoldPosition(), session(16, corporate_actions=(DividendPay("d"),)))
        self.assertEqual(event.account.shares, 0)
        self.assertEqual(event.dividend_cash, D("1.25"))
        self.assertEqual(event.equity, D("1001.25"))

    def test_ex_date_buyer_has_no_dividend(self):
        episode = TradingEpisode("exbuyer", task(2))
        episode.settle_next_session(TargetWeight(D(1)), session(corporate_actions=(DividendEx("d", D(1)),)))
        event = episode.settle_next_session(HoldPosition(), session(15, corporate_actions=(DividendPay("d"),)))
        self.assertEqual(event.dividend_cash, 0)
        self.assertEqual(event.equity, D(1000))

    def test_dividend_invalid_command_does_not_mutate_ledger(self):
        episode = TradingEpisode("atomic-ledger", task(3))
        episode.settle_next_session(TargetWeight(D(1)), session())
        with self.assertRaises(DomainError):
            episode.settle_next_session(HoldPosition(), session(15, corporate_actions=(DividendEx("d", D(1)), "split")))
        self.assertEqual(len(episode.events), 1)
        episode.settle_next_session(HoldPosition(), session(15, corporate_actions=(DividendEx("d", D(1)),)))
        episode.settle_next_session(HoldPosition(), session(16, corporate_actions=(DividendPay("d"),)))
        self.assertEqual(episode.account.cash, D(10))

    def test_duplicate_payment_rejected_atomically(self):
        episode = TradingEpisode("double-pay", task(3))
        episode.settle_next_session(TargetWeight(D(1)), session(corporate_actions=(DividendEx("d", D(1)),)))
        episode.settle_next_session(HoldPosition(), session(15, corporate_actions=(DividendPay("d"),)))
        with self.assertRaises(DomainError):
            episode.settle_next_session(HoldPosition(), session(16, corporate_actions=(DividendPay("d"),)))
        self.assertEqual(len(episode.events), 2)

    def test_invalid_model_actions_are_not_coerced(self):
        for weight in (True, "0.5", -0.1, 1.1, None):
            with self.subTest(weight=weight), self.assertRaises(DomainError):
                parse_action(json.dumps({"target_weight": weight, "rationale": "x"}))
        self.assertEqual(parse_action('{"target_weight":0.17,"rationale":"x"}').value, D("0.17"))

    def test_truncated_response_is_not_silent_hold(self):
        trader = OllamaTrader()
        with patch("quant_rsi.adapters.ollama_trader.urlopen") as opening:
            opening.return_value.__enter__.return_value.read.return_value = json.dumps({
                "done": True, "done_reason": "length", "message": {"content": '{}'}}).encode()
            with self.assertRaises(DomainError):
                trader.decide(MarketSnapshot("INTC", task().as_of, ()), Account(D(100), 0))
        with self.assertRaises(DomainError):
            OllamaTrader(base_url="https://example.com")

    def test_frozen_data_hash_and_release_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {"symbol": "INTC", "as_of": task().as_of.isoformat(), "sessions": [{
                "opens_at": session().opens_at.isoformat(), "closes_at": session().closes_at.isoformat(),
                "raw_open": "100", "raw_close": "100", "actions": []}], "records": [{
                    "record_id": "q2", "event_time": "2024-08-01T16:01:00-04:00",
                    "available_at": "2024-08-01T16:01:00-04:00", "content": "earnings", "source": "fixture"}]}
            data = root / "dataset.json"
            data.write_text(json.dumps(value))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"schema_version": "pilot_dataset.v1", "benchmark_ready": False,
                "files": [{"path": "dataset.json", "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}]}))
            market = FrozenPilotMarket(task(), manifest)
            self.assertEqual(market.snapshot("INTC", datetime.fromisoformat("2024-08-01T16:00:00-04:00")).records, ())
            self.assertEqual(len(market.snapshot("INTC", datetime.fromisoformat("2024-08-02T16:00:00-04:00")).records), 1)
            data.write_text("{}")
            with self.assertRaises(DomainError):
                FrozenPilotMarket(task(), manifest)
