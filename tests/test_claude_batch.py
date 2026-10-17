import argparse
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts.claude_batch import parse_usage, run


class ClaudeAdmissionTests(unittest.TestCase):
    def test_unknown_quota_cannot_be_treated_as_zero(self):
        for text in ("", "subscription active", "Current session: 5% used"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_usage(text)
        usage = parse_usage("Current session: 5% used · resets today\n"
                            "Current week (all models): 2% used · resets next week")
        self.assertEqual(usage["session"]["used_percent"], 5)
        self.assertEqual(usage["week"]["used_percent"], 2)
        usage = parse_usage("Current session: 5% used\nCurrent week (all models): 2% used\n"
                            "Current week (Fable): 75% used · resets next week")
        self.assertEqual(usage["Current week (Fable)"]["used_percent"], 75)

    def test_cooldown_blocks_before_starting_any_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dispatch-state.json").write_text(json.dumps({"finished_at": time.time()}))
            args = argparse.Namespace(root=root, name="b2", interval=300)
            with patch("scripts.claude_batch.subprocess.run") as process:
                with self.assertRaisesRegex(ValueError, "cooldown"):
                    run(args)
                process.assert_not_called()

    def test_quota_reserve_blocks_model_job(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(root=Path(directory), name="b2", interval=300,
                                      cli="claude", session_stop=65, week_stop=70)
            with patch("scripts.claude_batch.read_usage", return_value={
                    "session": {"used_percent": 65}, "week": {"used_percent": 2}}), \
                    patch("scripts.claude_batch.subprocess.run") as process:
                with self.assertRaisesRegex(ValueError, "reserve"):
                    run(args)
                process.assert_not_called()

    def test_successful_process_exit_does_not_hide_incomplete_agent_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = root / "prompt.md"
            request.write_text("fixture")
            args = argparse.Namespace(root=root, name="b1", interval=300, cli="claude",
                                      session_stop=65, week_stop=70, request=request,
                                      max_turns=1, timeout=30)

            def incomplete(*command, **kwargs):
                kwargs["stdout"].write(json.dumps({"subtype": "error_max_turns", "is_error": False,
                                                   "modelUsage": {"claude-opus-5-5": {}}}).encode())
                return SimpleNamespace(returncode=0)

            with patch("scripts.claude_batch.read_usage", return_value={
                    "session": {"used_percent": 5}, "week": {"used_percent": 2}}), \
                    patch("scripts.claude_batch.subprocess.run", side_effect=incomplete):
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    run(args)
            receipt = json.loads((root / "b1/dispatch.json").read_text())
            self.assertEqual(receipt["result_subtype"], "error_max_turns")
