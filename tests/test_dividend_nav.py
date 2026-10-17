from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import unittest

from test_domain import task, session
from quant_rsi.domain.corporate_actions import DividendEx, DividendPay
from quant_rsi.domain.evaluation import evaluate
from quant_rsi.domain.trading import TradingEpisode, TargetWeight, HoldPosition
from scripts.validate_benchmark import load, validate_environment, ConfigError


class DividendNAVTests(unittest.TestCase):
    def episode(self, horizon, version="single_asset_daily_v2"):
        return TradingEpisode("dividend-nav", replace(task(horizon), engine_contract=version))

    def test_ex_date_price_drop_is_offset_by_receivable_at_terminal(self):
        for version, terminal in (("single_asset_daily_v1", D(990)), ("single_asset_daily_v2", D(1000))):
            with self.subTest(version=version):
                episode = self.episode(2, version)
                episode.settle_next_session(TargetWeight(D(1)), session())
                event = episode.settle_next_session(HoldPosition(), session(
                    15, "99", "99", corporate_actions=(DividendEx("d", D(1)),)))
                self.assertEqual(event.equity, terminal)
                self.assertEqual(event.account.cash, 0)
                self.assertEqual(event.account.dividend_receivable, D(10) if version.endswith("v2") else D(0))
                self.assertEqual(evaluate(episode).net_return, D(0) if version.endswith("v2") else D("-0.01"))

    def test_selling_on_ex_day_preserves_claim_and_payment_does_not_double_count(self):
        episode = self.episode(3)
        episode.settle_next_session(TargetWeight(D(1)), session())
        ex = episode.settle_next_session(TargetWeight(D(0)), session(
            15, "99", "99", corporate_actions=(DividendEx("d", D(1)),)))
        self.assertEqual(ex.account.shares, 0)
        self.assertEqual(ex.account.cash, 990)
        self.assertEqual(ex.account.dividend_receivable, 10)
        paid = episode.settle_next_session(HoldPosition(), session(
            16, "99", "99", corporate_actions=(DividendPay("d"),)))
        self.assertEqual(paid.dividend_cash, 10)
        self.assertEqual(paid.account.cash, 1000)
        self.assertEqual(paid.account.dividend_receivable, 0)
        self.assertEqual(paid.equity, ex.equity)

    def test_unpaid_claim_does_not_become_cash_for_orders(self):
        episode = self.episode(2)
        episode.settle_next_session(TargetWeight(D(1)), session())
        # Large synthetic entitlement tests the affordability boundary directly.
        event = episode.settle_next_session(TargetWeight(D(1)), session(
            15, "100", "100", corporate_actions=(DividendEx("d", D(20)),)))
        self.assertEqual(event.account.cash, 0)
        self.assertEqual(event.account.shares, 10)
        self.assertEqual(event.account.dividend_receivable, 200)

    def test_config_cannot_pair_legacy_valuation_with_new_engine(self):
        root = Path(__file__).resolve().parents[1]
        env = load(root / "benchmarks/environments/us-equity-daily-v2.toml")
        validate_environment(env)
        env["valuation"]["terminal"] = "mark_to_market"
        with self.assertRaises(ConfigError):
            validate_environment(env)
