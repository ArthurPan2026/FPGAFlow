from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fpga_flow.runner import run_command


class CommandRunnerTests(unittest.TestCase):
    def test_runs_argv_without_shell_and_records_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "run"
            result = run_command(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", "x;touch SHOULD_NOT_EXIST"],
                cwd=root,
                env={"FLOW_TEST_VALUE": "ready"},
                log_dir=logs,
            )
            self.assertTrue(result.succeeded)
            self.assertEqual(result.stdout.strip(), "x;touch SHOULD_NOT_EXIST")
            self.assertFalse((root / "SHOULD_NOT_EXIST").exists())
            self.assertEqual((logs / "stdout.log").read_text(encoding="utf-8").strip(), result.stdout.strip())
            command = json.loads((logs / "command.json").read_text(encoding="utf-8"))
            self.assertEqual(command["argv"], list(result.command))
            self.assertEqual(command["env"]["FLOW_TEST_VALUE"], "ready")

    def test_redacts_secrets_from_command_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "run"
            run_command([sys.executable, "-c", "pass"], cwd=root,
                        env={"API_TOKEN": "never-log-this", "SAFE_MODE": "1"}, log_dir=logs)
            record = json.loads((logs / "command.json").read_text(encoding="utf-8"))
            self.assertEqual(record["env"]["API_TOKEN"], "<redacted>")
            self.assertEqual(record["env"]["SAFE_MODE"], "1")
            self.assertNotIn("never-log-this", (logs / "command.json").read_text(encoding="utf-8"))

    def test_timeout_is_reported_and_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_command([sys.executable, "-c", "import time; time.sleep(1)"],
                                cwd=root, timeout_seconds=0.05, log_dir=root / "timeout")
            self.assertFalse(result.succeeded)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.exit_code, 124)
            saved = json.loads((root / "timeout/execution.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["timed_out"])

    @unittest.skipUnless(os.name == "nt", "requires Windows batch command semantics")
    def test_windows_batch_command_and_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="FPGA Flow Windows ") as tmp:
            root = Path(tmp)
            command_file = root / "Toolchain Wrapper.cmd"
            command_file.write_text("@echo off\r\necho ARG=%1 %2\r\n", encoding="ascii")
            result = run_command([str(command_file), "hello", "world"], cwd=root)
            self.assertTrue(result.succeeded, result.stderr)
            self.assertEqual(result.stdout.strip(), "ARG=hello world")

    def test_rejects_empty_command_and_missing_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                run_command([], cwd=root)
            with self.assertRaises(ValueError):
                run_command([sys.executable], cwd=root / "missing")


if __name__ == "__main__":
    unittest.main()
