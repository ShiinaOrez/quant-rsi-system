import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.validate_benchmark import ConfigError
from scripts.validate_suite import validate_suite
from quant_rsi.adapters.toml_benchmark import load_definition


ROOT = Path(__file__).resolve().parents[1]
SUITE = Path("suites/nasdaq-diagnostic-v1.toml")


class BenchmarkSuiteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.benchmarks = Path(self.directory.name) / "benchmarks"
        shutil.copytree(ROOT / "benchmarks", self.benchmarks)
        self.suite = self.benchmarks / SUITE

    def change(self, relative, before, after):
        path = self.benchmarks / relative
        original = path.read_text()
        self.assertIn(before, original)
        path.write_text(original.replace(before, after, 1))

    def test_repository_suite_links_twenty_definitions(self):
        report = validate_suite(self.suite)
        self.assertEqual((report["tasks"], report["new_cases"]), (20, 18))
        self.assertFalse(report["data_materialized"])

    def test_existing_trader_adapter_loads_case_without_private_notes(self):
        task = self.benchmarks / "tasks/msft-2022-12-30-60d.toml"
        before = load_definition(task)
        notes = self.benchmarks / "assessor/msft-2022-12-30-60d.toml"
        notes.write_text("private diagnostic content must not enter trader definition")
        self.assertEqual(before, load_definition(task))

    @unittest.skipUnless(importlib.util.find_spec("exchange_calendars"),
                         "optional market dependencies not installed")
    def test_sixty_future_sessions_use_exchange_calendar(self):
        report = validate_suite(self.suite, with_calendar=True)
        self.assertEqual(report["windows"][0]["first"], "2023-01-03")
        self.assertEqual(report["windows"][0]["terminal"], "2023-03-29")
        self.change(SUITE, 'terminal_session = "2023-03-29"',
                    'terminal_session = "2023-03-28"')
        with self.assertRaisesRegex(ConfigError, "session differs"):
            validate_suite(self.suite, with_calendar=True)

    def test_future_document_is_rejected(self):
        self.change("assessor/msft-2022-12-30-60d.toml",
                    'publication_date = "2022-10-25"',
                    'publication_date = "2023-01-25"')
        with self.assertRaisesRegex(ConfigError, "before cutoff"):
            validate_suite(self.suite)

    def test_diagnostic_notes_cannot_claim_agent_visibility(self):
        self.change("assessor/msft-2022-12-30-60d.toml",
                    "agent_visible = false", "agent_visible = true")
        with self.assertRaisesRegex(ConfigError, "remain private"):
            validate_suite(self.suite)

    def test_identity_mismatch_is_rejected(self):
        self.change("data-plans/msft-2022-12-30-60d.toml",
                    'symbol = "MSFT"', 'symbol = "AMZN"')
        with self.assertRaisesRegex(ConfigError, "symbol differs"):
            validate_suite(self.suite)

    def test_wrong_dst_offset_is_rejected(self):
        self.change("tasks/msft-2023-06-30-60d.toml",
                    "16:00:00-04:00", "16:00:00-05:00")
        with self.assertRaisesRegex(ConfigError, "offset"):
            validate_suite(self.suite)

    def test_grid_cannot_be_replaced_by_duplicate_case(self):
        self.change(SUITE, 'task = "../tasks/msft-2023-06-30-60d.toml"',
                    'task = "../tasks/msft-2022-12-30-60d.toml"')
        with self.assertRaisesRegex(ConfigError, "duplicate task"):
            validate_suite(self.suite)


if __name__ == "__main__":
    unittest.main()
