from datetime import datetime
import unittest

from quant_rsi.application.history import HistoryItem, PointInTimeHistory
from quant_rsi.domain.market import InformationRecord
from quant_rsi.domain.shared import DomainError


def timestamp(day):
    return datetime.fromisoformat(f"2024-07-{day:02d}T16:00:00-04:00")


def item(identity, day, text, symbol="INTC"):
    return HistoryItem(InformationRecord(identity, symbol, timestamp(day), timestamp(day), text, "fixture"), "filing")


class UnfilteredProvider:
    # Deliberately returns records for the wrong symbol and future cutoffs.
    def __init__(self, *items):
        self.items = items

    def scan(self, symbol, available_through):
        return self.items


class HistoryBoundaryTests(unittest.TestCase):
    def test_model_cannot_override_environment_clock_or_instrument(self):
        from jsonschema.exceptions import ValidationError
        from quant_rsi.tools.history import HistoryTools

        events = []
        tools = HistoryTools(PointInTimeHistory(UnfilteredProvider(item("original", 1, "income 10")),
                                               "INTC", timestamp(11)), capture=events.append)
        for arguments in ({"decision_time": timestamp(12).isoformat()}, {"symbol": "AAPL"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                tools.invoke("search_history", arguments)
        record = tools.invoke("search_history", {})["items"][0]
        self.assertEqual(tools.invoke("read_history", {"record_id": record["record_id"]})["content"], "income 10")
        self.assertEqual([event["tool"] for event in events], ["search_history", "read_history"])

    def test_future_ids_and_missing_ids_are_indistinguishable(self):
        view = PointInTimeHistory(UnfilteredProvider(item("future", 12, "recast"),
                                                    item("other", 1, "private", "AAPL")), "INTC", timestamp(11))
        self.assertEqual(view.search()["total"], 0)
        for identity in ("future", "other", "missing"):
            with self.subTest(identity=identity), self.assertRaisesRegex(DomainError, "record not available"):
                view.read(identity)

    def test_amendment_does_not_replace_original_and_pages_cover_history(self):
        archive = UnfilteredProvider(item("original", 1, "income 10"), item("amended", 12, "income 8"))
        early = PointInTimeHistory(archive, "INTC", timestamp(11))
        late = PointInTimeHistory(archive, "INTC", timestamp(12))
        self.assertEqual(early.search(query="income")["total"], 1)
        first = late.search(limit=1)
        second = late.search(offset=first["next_offset"], limit=1)
        self.assertEqual([first["items"][0]["record_id"], second["items"][0]["record_id"]], ["amended", "original"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(late.read("original", limit=5)["content"] + late.read("original", offset=5)["content"], "income 10")
        self.assertEqual(early.read("original")["content"], "income 10")

    def test_boolean_and_oversized_pages_rejected(self):
        view = PointInTimeHistory(UnfilteredProvider(), "INTC", timestamp(11))
        for arguments in ({"limit": True}, {"offset": -1}, {"limit": 51}):
            with self.subTest(arguments=arguments), self.assertRaises(DomainError):
                view.search(**arguments)
