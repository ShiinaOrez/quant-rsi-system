from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .shared import aware, number, require


@dataclass(frozen=True)
class InformationRecord:
    record_id: str
    symbol: str
    event_time: datetime
    available_at: datetime
    content: str
    source: str

    def __post_init__(self):
        aware(self.event_time)
        aware(self.available_at)
        require(bool(self.record_id) and bool(self.symbol) and bool(self.source),
                "record identity and provenance required")


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    decision_time: datetime
    records: tuple[InformationRecord, ...]

    def __post_init__(self):
        aware(self.decision_time)
        require(isinstance(self.records, tuple), "immutable records required")
        require(all(r.symbol == self.symbol and r.available_at <= self.decision_time
                    for r in self.records), "snapshot contains unavailable or wrong-symbol record")


def snapshot_at(symbol: str, decision_time: datetime,
                records: tuple[InformationRecord, ...]) -> MarketSnapshot:
    aware(decision_time)
    return MarketSnapshot(symbol, decision_time, tuple(
        r for r in records if r.symbol == symbol and r.available_at <= decision_time))


@dataclass(frozen=True)
class TradingSession:
    symbol: str
    opens_at: datetime
    closes_at: datetime
    raw_open: Decimal | None
    raw_close: Decimal | None
    tradable: bool = True
    corporate_actions: tuple[str, ...] = ()

    def __post_init__(self):
        aware(self.opens_at)
        aware(self.closes_at)
        require(self.opens_at < self.closes_at, "session open must precede close")
        require(type(self.tradable) is bool, "tradable must be boolean")
        for price in (self.raw_open, self.raw_close):
            if price is not None:
                number(price)
                require(price > 0, "price must be positive")
