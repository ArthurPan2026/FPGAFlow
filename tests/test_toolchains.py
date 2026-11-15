from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

from fpga_flow.toolchains import probe_toolchain


class ToolchainProbeTests(unittest.TestCase):
    def test_probe_and_version_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_toolchain("python", sys.executable, ["--version"], "Python ", cwd=Path(tmp))
            self.assertTrue(result.ok, result.detail)
            self.assertIn("Python", result.reported_version or "")

    def test_mismatched_version_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_toolchain("python", sys.executable, ["--version"], "impossible-version", cwd=Path(tmp))
            self.assertEqual(result.status, "mismatch")

    def test_missing_executable_is_reported_without_launching_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_toolchain("missing", "fpga-flow-no-such-tool", cwd=Path(tmp))
            self.assertEqual(result.status, "missing")
            self.assertIsNone(result.resolved_command)


if __name__ == "__main__":
    unittest.main()
