from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fpga_flow.runner import run_command


class CommandRunnerTests(unittest.TestCase):
    def test_default_progress_interval_is_twenty_seconds(self) -> None:
        import inspect
        parameter = inspect.signature(run_command).parameters["progress_interval_seconds"]
        self.assertEqual(parameter.default, 20.0)

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

    def test_logs_are_visible_before_the_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "stream"
            observed_live_output = []

            def on_progress(stats):
                path = logs / "stdout.log"
                if path.is_file():
                    observed_live_output.append(path.read_text(encoding="utf-8"))

            result = run_command(
                [sys.executable, "-c",
                 "import time; print('first', flush=True); time.sleep(.5); print('last', flush=True)"],
                cwd=root, log_dir=logs, timeout_seconds=3,
                on_progress=on_progress, progress_interval_seconds=0.05,
            )
            self.assertTrue(result.succeeded, result.stderr)
            self.assertTrue(any("first" in value and "last" not in value for value in observed_live_output))
            self.assertEqual((logs / "stdout.log").read_text(encoding="utf-8"), "first\nlast\n")

    @unittest.skipUnless(os.name == "posix", "process groups are validated on POSIX")
    def test_timeout_terminates_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "print(child.pid, flush=True); time.sleep(30)"
            )
            result = run_command([sys.executable, "-c", code], cwd=root,
                                 timeout_seconds=0.3, log_dir=root / "tree")
            self.assertTrue(result.timed_out)
            child_pid = int(result.stdout.strip().splitlines()[0])
            for _ in range(30):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                stat = Path(f"/proc/{child_pid}/stat")
                if stat.is_file() and stat.read_text().split()[2] == "Z":
                    break
                time.sleep(0.05)
            else:
                self.fail("timeout left a child process running")

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
