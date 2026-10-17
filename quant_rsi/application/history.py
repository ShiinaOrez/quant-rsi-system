"""Read-only history queries bound to the environment's instrument and clock."""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Protocol

from quant_rsi.domain.market import InformationRecord
from quant_rsi.domain.shared import aware, require


KINDS = frozenset({"ohlcv", "filing", "financial_fact", "corporate_action"})


@dataclass(frozen=True)
class HistoryItem:
    record: InformationRecord
    kind: str

    def __post_init__(self):
        require(self.kind in KINDS, "unsupported history kind")


class InformationArchive(Protocol):
    def scan(self, symbol: str, available_through: datetime) -> Iterable[HistoryItem]:
        """Supply immutable versions; callers also enforce the visibility boundary."""
        ...


class PointInTimeHistory:
    """Create a fresh view per decision; model arguments cannot advance its clock.

    Search returns provenance and IDs. Read returns paginated original content.
    Future and nonexistent IDs have the same error. Later amendments remain
    separate records; this service never chooses a latest financial fact.
    """

    def __init__(self, archive: InformationArchive, symbol: str, decision_time: datetime):
        aware(decision_time)
        require(bool(symbol), "instrument required")
        self._archive, self._symbol, self._clock = archive, symbol, decision_time

    def _visible(self):
        # A provider implementation cannot accidentally leak future records.
        items = [item for item in self._archive.scan(self._symbol, self._clock)
                 if item.record.symbol == self._symbol and item.record.available_at <= self._clock]
        keys = [item.record.record_id for item in items]
        require(len(keys) == len(set(keys)), "duplicate visible history identity")
        return sorted(items, key=lambda item: (item.record.available_at, item.record.record_id), reverse=True)

    @staticmethod
    def _page(offset, limit, maximum):
        require(type(offset) is int and offset >= 0, "nonnegative integer offset required")
        require(type(limit) is int and 1 <= limit <= maximum, "invalid page size")

    @staticmethod
    def _metadata(item):
        record = item.record
        return {"record_id": record.record_id, "kind": item.kind, "symbol": record.symbol,
                "event_time": record.event_time.isoformat(), "available_at": record.available_at.isoformat(),
                "source": record.source, "content_characters": len(record.content)}

    def search(self, *, kind=None, query="", offset=0, limit=20):
        self._page(offset, limit, 50)
        require(kind is None or kind in KINDS, "unsupported history kind")
        require(type(query) is str and len(query) <= 200, "query must be at most 200 characters")
        needle = query.casefold()
        matched = [item for item in self._visible()
                   if (kind is None or item.kind == kind)
                   and (not needle or needle in item.record.content.casefold())]
        end = offset + limit
        return {"decision_time": self._clock.isoformat(), "total": len(matched),
                "items": [self._metadata(item) for item in matched[offset:end]],
                "next_offset": end if end < len(matched) else None}

    def read(self, record_id, *, offset=0, limit=4000):
        self._page(offset, limit, 8000)
        require(type(record_id) is str and 0 < len(record_id) <= 256, "record ID required")
        item = next((item for item in self._visible() if item.record.record_id == record_id), None)
        require(item is not None, "record not available")
        end = offset + limit
        return {**self._metadata(item), "decision_time": self._clock.isoformat(),
                "offset": offset, "content": item.record.content[offset:end],
                "next_offset": end if end < len(item.record.content) else None}
