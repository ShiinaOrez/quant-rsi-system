import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.validate_benchmark import ConfigError, load, validate_environment, validate_task

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks/tasks/aapl-2023-midyear-60d.toml"
ENV = ROOT / "benchmarks/environments/us-equity-daily-v1.toml"


class BenchmarkConfigTests(unittest.TestCase):
    def test_all_repository_tasks_follow_contract(self):
        for path in sorted((ROOT / "benchmarks/tasks").glob("*.toml")):
            with self.subTest(task=path.name):
                validate_task(path)

    def test_example_is_valid_draft_but_not_ready(self):
        self.assertEqual(validate_task(TASK)["status"], "draft")
        with self.assertRaisesRegex(ConfigError, "draft"):
            validate_task(TASK, require_ready=True)

    def test_environment_rejects_unknown_and_noncausal_rules(self):
        env = load(ENV)
        for section, field, value in [
            ("execution", "timing", "same_session_close"),
            ("observation", "visibility", "all_history"),
            ("execution", "commission_bps", -1),
            ("action", "max_weight", 2),
            ("reproducibility", "seed", True),
            ("indicators", "future_window", 10),
        ]:
            with self.subTest(section=section, field=field):
                bad = copy.deepcopy(env)
                bad[section][field] = value
                with self.assertRaises(ConfigError):
                    validate_environment(bad)

    def test_task_rejects_invalid_timestamps_and_goals(self):
        raw = TASK.read_text().replace(
            '../environments/us-equity-daily-v1.toml', ENV.as_posix())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.toml"
            for before, after in [
                ('2023-06-30T16:00:00-04:00', '2023-06-30T16:00:00'),
                ('horizon_sessions = 60', 'horizon_sessions = 0'),
                ('max_drawdown_lte = 0.10', 'max_drawdown_lte = 1.10'),
            ]:
                path.write_text(raw.replace(before, after))
                with self.assertRaises(ConfigError):
                    validate_task(path)

    def test_ready_requires_matching_manifest_hash(self):
        raw = TASK.read_text().replace(
            '../environments/us-equity-daily-v1.toml', ENV.as_posix())
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "manifest.json"
            manifest.write_text('{"files": []}\n')
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            raw = raw.replace('status = "draft"', 'status = "ready"')
            raw = raw.replace('manifest = "UNBOUND"', 'manifest = "manifest.json"')
            raw = raw.replace('sha256 = "UNBOUND"', f'sha256 = "{digest}"')
            path = Path(folder) / "task.toml"
            path.write_text(raw)
            self.assertEqual(validate_task(path, require_ready=True)["status"], "ready")
            manifest.write_text('{"files": ["changed"]}\n')
            with self.assertRaisesRegex(ConfigError, "mismatch"):
                validate_task(path, require_ready=True)


if __name__ == "__main__":
    unittest.main()
