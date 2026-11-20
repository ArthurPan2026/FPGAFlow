from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fpga_flow.config import load_profile
from fpga_flow.engine import FlowEngine, FlowError
from fpga_flow.external import ExternalDependencyError, inspect_dependency


@unittest.skipUnless(shutil.which("git"), "Git is required")
class ExternalDependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "external"
        self.root = base / "project"
        self.repo.mkdir()
        self.root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.invalid")
        (self.repo / "hdl").mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "hdl/core.sv").write_text("module core; endmodule\n")
        (self.repo / "docs/readme.md").write_text("docs\n")
        self._git("add", ".")
        self._git("commit", "-qm", "initial")
        revision = self._git("rev-parse", "HEAD").strip()
        (self.root / "build.py").write_text(
            "from pathlib import Path\nimport os\np=Path('out/result');p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(os.environ['FIXTURE_VENDOR_ROOT'])\n")
        lines = ["schema_version = 1", "[project]", 'name = "fixture"',
                 "[external_dependencies.vendor]", 'path = "../external"',
                 f'revision = "{revision}"', 'inputs = ["hdl/**"]',
                 'ignored_committed_paths = ["docs/*.md"]',
                 'environment_variable = "FIXTURE_VENDOR_ROOT"',
                 "[stages.logic]", 'kind = "build"', 'inputs = ["build.py"]',
                 'external_inputs = ["vendor"]', 'outputs = ["out/result"]',
                 "command = " + json.dumps([sys.executable, "build.py"])]
        self.config = self.root / "fpga-flow.toml"
        self.config.write_text("\n".join(lines) + "\n")
        self.engine = FlowEngine(load_profile(self.config))

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout

    def test_selected_inputs_invalidate_cache_and_docs_commit_does_not(self) -> None:
        self.engine.build("logic")
        self.assertEqual((self.root / "out/result").read_text(), str(self.repo.resolve()))
        self.assertTrue(self.engine.build("logic")["stages"][0]["cache_hit"])
        (self.repo / "docs/readme.md").write_text("updated docs\n")
        self._git("add", ".")
        self._git("commit", "-qm", "docs")
        self.assertTrue(self.engine.build("logic")["stages"][0]["cache_hit"])
        (self.repo / "hdl/core.sv").write_text("module core; wire x; endmodule\n")
        with self.assertRaises(FlowError) as caught:
            self.engine.build("logic")
        self.assertEqual(caught.exception.code, "FLOW-EXTERNAL-DEPENDENCY")
        self._git("add", ".")
        self._git("commit", "-qm", "rtl")
        with self.assertRaises(FlowError) as caught:
            self.engine.build("logic")
        self.assertEqual(caught.exception.code, "FLOW-EXTERNAL-DEPENDENCY")

    def test_untracked_checkout_changes_are_rejected(self) -> None:
        (self.repo / "untracked.txt").write_text("not committed")
        with self.assertRaises(ExternalDependencyError):
            inspect_dependency(self.engine.profile.external_dependencies["vendor"])

    def test_external_source_mutation_during_stage_does_not_pass(self) -> None:
        (self.root / "build.py").write_text(
            "from pathlib import Path\nimport os\n"
            "Path(os.environ['FIXTURE_VENDOR_ROOT'],'hdl/core.sv').write_text('changed')\n"
            "p=Path('out/result');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('result')\n")
        with self.assertRaises(FlowError) as caught:
            self.engine.build("logic")
        self.assertEqual(caught.exception.code, "FLOW-EXTERNAL-DEPENDENCY")
        self.assertEqual(self.engine.store.latest_stage("logic")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
