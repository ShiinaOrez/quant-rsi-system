from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .shared import DatasetSplit, aware, number, require


@dataclass(frozen=True)
class ProfitGoal:
    net_return_gte: Decimal
    max_drawdown_lte: Decimal

    def __post_init__(self):
        number(self.net_return_gte)
        number(self.max_drawdown_lte)
        require(self.max_drawdown_lte <= 1, "drawdown limit must be <= 1")


@dataclass(frozen=True)
class ExecutionRules:
    commission_bps: Decimal
    slippage_bps: Decimal

    def __post_init__(self):
        for value in (self.commission_bps, self.slippage_bps):
            number(value)
            require(value < 10000, "cost must be below 10000 bps")


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    symbol: str
    currency: str
    as_of: datetime
    horizon_sessions: int
    initial_cash: Decimal
    split: DatasetSplit
    goal: ProfitGoal
    execution: ExecutionRules
    configuration_digest: str
    engine_contract: str = "single_asset_daily_v1"

    def __post_init__(self):
        require(self.engine_contract in ("single_asset_daily_v1", "single_asset_daily_v2"),
                "unsupported daily execution contract")
        require(bool(self.task_id.strip()) and bool(self.symbol.strip()), "identity required")
        require(self.currency == "USD", "v0.1 supports USD only")
        aware(self.as_of)
        require(type(self.horizon_sessions) is int and self.horizon_sessions > 0,
                "positive integer horizon required")
        number(self.initial_cash)
        require(self.initial_cash > 0, "positive initial cash required")
        require(isinstance(self.split, DatasetSplit), "typed dataset split required")
        require(len(self.configuration_digest) == 64
                and all(c in "0123456789abcdef" for c in self.configuration_digest),
                "configuration SHA-256 required")
