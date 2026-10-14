from dataclasses import dataclass

from quant_rsi.domain.market import InformationRecord, TradingSession, snapshot_at
from quant_rsi.domain.trading import TargetWeight


@dataclass(frozen=True)
class InMemoryHistory:
    records: tuple[InformationRecord, ...]

    def snapshot(self, symbol, decision_time):
        return snapshot_at(symbol, decision_time, self.records)


@dataclass(frozen=True)
class ExplicitSessions:
    """Synthetic/test adapter; not an exchange holiday calendar."""
    sessions: tuple[TradingSession, ...]

    def next_sessions(self, task):
        return self.sessions


@dataclass(frozen=True)
class ConstantWeightPolicy:
    target: TargetWeight

    def decide(self, snapshot, account):
        return self.target
