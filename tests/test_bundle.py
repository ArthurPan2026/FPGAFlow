from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from fpga_flow.config import ProfileError, load_profile
from fpga_flow.engine import FlowEngine, FlowError
from fpga_flow.runner import CommandResult


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        for name in ("hw", "platform", "app", "boot"):
            (self.root / "src" / name).write_text(name, encoding="utf-8")
        (self.root / "build.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "p=Path(sys.argv[2]); p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(Path(sys.argv[1]).read_text())\n", encoding="utf-8")
        lines = [
            "schema_version = 1", "[project]", 'name = "generic-fixture"',
            "[flow]", 'output_dir = "out/flow"', 'default_target = "board"',
            "[bundle]", 'output_dir = "out/current"', "[bundle.artifacts]",
            'bitstream = "out/build/hardware.bit"', 'fsbl = "out/build/fsbl.elf"',
            'app = "out/build/app.elf"', 'boot_bin = "out/build/boot.bin"',
            "[toolchains.python]", "command = " + json.dumps(sys.executable),
            "[targets.board]", 'family = "generic"',
        ]
        for stage, source, output, needs in (
            ("logic", "hw", "hardware.bit", []),
            ("runtime", "platform", "fsbl.elf", ["logic"]),
            ("software", "app", "app.elf", ["runtime"]),
            ("image", "boot", "boot.bin", ["logic", "runtime", "software"]),
        ):
            lines += [f"[stages.{stage}]", 'kind = "build"',
                      "needs = " + json.dumps(needs),
                      "inputs = " + json.dumps([f"src/{source}", "build.py"]),
                      "outputs = " + json.dumps([f"out/build/{output}"]),
                      "command = " + json.dumps([sys.executable, "build.py", f"src/{source}", f"out/build/{output}"]),
                      'toolchains = ["python"]']
        self.config = self.root / "fpga-flow.toml"
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.engine = FlowEngine(load_profile(self.config))

    def _bundle(self) -> dict:
        return json.loads((self.root / "out/current/bundle.json").read_text(encoding="utf-8"))

    def test_generates_bundle_from_nonstandard_stage_names(self) -> None:
        self.engine.build("image", with_deps=True)
        data = self._bundle()
        self.assertEqual(data["schema_version"], 3)
        self.assertEqual(data["artifacts"]["bitstream"]["stage"], "logic")
        self.assertEqual((self.root / "out/current/BOOT.BIN").read_text(), "boot")
        self.assertFalse(any(data["verification"].values()))

    def test_new_hardware_cannot_publish_with_stale_downstream_results(self) -> None:
        self.engine.build("image", with_deps=True)
        old = self._bundle()
        (self.root / "src/hw").write_text("new hardware", encoding="utf-8")
        self.engine.build("logic")
        self.assertEqual(self._bundle()["bundle_id"], old["bundle_id"])
        with self.assertRaises(FlowError) as caught:
            self.engine._refresh_bundle("board", required=True)
        self.assertEqual(caught.exception.code, "FLOW-BUNDLE-STALE")
        self.engine.build("image", with_deps=True)
        self.assertNotEqual(self._bundle()["bundle_id"], old["bundle_id"])
        self.assertEqual((self.root / "out/current/bitstream.bit").read_text(), "new hardware")

    def test_verification_is_not_inherited_by_a_different_bundle(self) -> None:
        self.engine.build("image", with_deps=True)
        bundle = self._bundle()
        bundle["verification"]["qspi_readback_verified"] = True
        (self.root / "out/current/bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        (self.root / "src/app").write_text("new software", encoding="utf-8")
        self.engine.build("image", with_deps=True)
        updated = self._bundle()
        self.assertNotEqual(bundle["bundle_id"], updated["bundle_id"])
        self.assertFalse(updated["verification"]["qspi_readback_verified"])

    def test_bundle_configuration_rejects_unknown_producer(self) -> None:
        source = self.config.read_text().replace('fsbl = "out/build/fsbl.elf"', 'fsbl = "out/build/missing.elf"')
        self.config.write_text(source)
        with self.assertRaises(ProfileError):
            load_profile(self.config)

    def test_release_rejects_unverified_qspi_before_running_stages(self) -> None:
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write('\n[stages.flash]\nkind = "deploy"\nrunner = "amd.qspi.program"\n'
                         'target = "board"\nneeds = ["image"]\noptions = {verify = false}\n')
        engine = FlowEngine(load_profile(self.config))
        with self.assertRaises(FlowError) as caught:
            engine.release(target="board", deploy=True, deploy_stage="flash")
        self.assertEqual(caught.exception.code, "FLOW-RELEASE-QSPI-VERIFY")
        self.assertFalse((self.root / "out/build").exists())

    def test_successful_qspi_records_bundle_identity_and_readback(self) -> None:
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write('\n[stages.flash]\nkind = "deploy"\nrunner = "amd.qspi.program"\n'
                         'target = "board"\nneeds = ["image", "runtime"]\noptions = {verify = true}\n')
        class FakeFlash:
            def execute(self, stage, profile, run_dir, target):
                return CommandResult(("fake", "-verify"), profile.root, "start", "end", 0, 0,
                                     False, "Flash Operation Successful\nVerify Operation successful.\n", "")
        engine = FlowEngine(load_profile(self.config), providers={"amd.qspi.program": FakeFlash()})
        engine.build("image", with_deps=True)
        result = engine.deploy("board", stage_name="flash")
        bundle = self._bundle()
        run_id = result["stages"][0]["run_id"]
        self.assertTrue(bundle["verification"]["qspi_programmed"])
        self.assertTrue(bundle["verification"]["qspi_readback_verified"])
        self.assertFalse(bundle["verification"]["cold_boot_verified"])
        self.assertEqual(bundle["verification_evidence"]["qspi_readback_verified"], run_id)
        selection = json.loads((engine.store.run_path(run_id).parent / "bundle-selection.json").read_text())
        self.assertEqual(selection["bundle_id"], bundle["bundle_id"])

    def test_cold_boot_report_is_bound_to_the_programmed_bundle(self) -> None:
        (self.root / "cold.py").write_text(
            "from pathlib import Path\nimport json\n"
            "b=json.loads(Path('out/current/bundle.json').read_text())\n"
            "p=Path('out/board/cold.json');p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(json.dumps({'status':'passed','cold_boot_verified':True,'bundle_id':b['bundle_id']}))\n")
        with self.config.open("a", encoding="utf-8") as stream:
            stream.write('\n[stages.flash]\nkind = "deploy"\nrunner = "amd.qspi.program"\n'
                         'target = "board"\nneeds = ["image", "runtime"]\noptions = {verify = true}\n'
                         '\n[stages.cold]\nkind = "validate"\ntarget = "board"\nneeds = ["flash"]\n'
                         f'command = [{json.dumps(sys.executable)}, "cold.py"]\n'
                         'inputs = ["cold.py"]\noutputs = ["out/board/cold.json"]\n'
                         'options = {bundle_verification = "cold_boot_verified", bundle_evidence = "out/board/cold.json"}\n'
                         '\n[suites]\ncold = ["cold"]\n')
        class FakeFlash:
            def execute(self, stage, profile, run_dir, target):
                return CommandResult(("fake", "-verify"), profile.root, "start", "end", 0, 0,
                                     False, "Flash Operation Successful\nVerify Operation successful.\n", "")
        engine = FlowEngine(load_profile(self.config), providers={"amd.qspi.program": FakeFlash()})
        engine.build("image", with_deps=True)
        engine.deploy("board", stage_name="flash")
        result = engine.validate("cold", target="board")
        bundle = self._bundle()
        self.assertTrue(bundle["verification"]["cold_boot_verified"])
        self.assertEqual(bundle["verification_evidence"]["cold_boot_verified"], result["stages"][0]["run_id"])

    def test_input_modified_during_execution_is_not_recorded_as_success(self) -> None:
        script = self.root / "build.py"
        script.write_text("from pathlib import Path\nPath('src/hw').write_text('mutated')\n"
                          "p=Path('out/build/hardware.bit');p.parent.mkdir(parents=True,exist_ok=True);p.write_text('bit')\n")
        with self.assertRaises(FlowError) as caught:
            self.engine.build("logic")
        self.assertEqual(caught.exception.code, "FLOW-INPUT-CHANGED")
        self.assertFalse((self.root / "out/current/bundle.json").exists())


if __name__ == "__main__":
    unittest.main()
