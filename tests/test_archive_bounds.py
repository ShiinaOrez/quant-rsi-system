from pathlib import Path
import tempfile
import unittest

from scripts.archive_bounds import suite_bounds


class ArchiveMetadataTests(unittest.TestCase):
    def fixture(self, root, cutoff):
        suites, tasks = root / "suites", root / "tasks"
        suites.mkdir()
        tasks.mkdir()
        (tasks / "future.toml").write_text('id="future"\n[instrument]\nsymbol="INTC"\n'
                                         f'[episode]\nas_of="{cutoff}"\nhorizon_sessions=60\n')
        suite = suites / "suite.toml"
        suite.write_text('legacy_tasks=["../tasks/future.toml"]\ncases=[]\nexpected_task_count=1\n')
        return suite

    def test_future_task_does_not_inherit_hardcoded_2024_download_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = self.fixture(Path(directory), "2025-12-31T16:00:00-05:00")
            result = suite_bounds(suite, "INTC")
            self.assertTrue(result["through"].startswith("2026-"))
            self.assertEqual(result["tasks"][0]["first_session"], "2026-01-02")

    def test_half_day_cannot_be_treated_as_1600_close(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = self.fixture(Path(directory), "2025-12-24T16:00:00-05:00")
            with self.assertRaisesRegex(ValueError, "cutoff differs"):
                suite_bounds(suite, "INTC")
