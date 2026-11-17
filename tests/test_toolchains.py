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

    def test_version_banner_does_not_hide_a_headless_display_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import sys; print('Vitis v2025.2.1'); "
                "print('Error: Cannot open DISPLAY', file=sys.stderr)"
            )
            result = probe_toolchain(
                "vitis", sys.executable, ["-c", script], "Vitis v2025.2.1",
                cwd=Path(tmp),
            )
            self.assertEqual(result.status, "warning")
            self.assertTrue(result.ok)
            self.assertIn("DISPLAY", result.detail)

    def test_other_error_output_overrides_a_matching_version_banner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import sys; print('Vitis v2025.2.1'); "
                "print('Error: license checkout failed', file=sys.stderr)"
            )
            result = probe_toolchain(
                "vitis", sys.executable, ["-c", script], "Vitis v2025.2.1",
                cwd=Path(tmp),
            )
            self.assertEqual(result.status, "error")
            self.assertFalse(result.ok)

    def test_missing_executable_is_reported_without_launching_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_toolchain("missing", "fpga-flow-no-such-tool", cwd=Path(tmp))
            self.assertEqual(result.status, "missing")
            self.assertIsNone(result.resolved_command)


if __name__ == "__main__":
    unittest.main()
