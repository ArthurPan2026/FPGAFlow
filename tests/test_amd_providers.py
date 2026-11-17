from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from types import MappingProxyType

from fpga_flow.models import FlowConfig, StageConfig, ToolchainConfig
from fpga_flow.providers.amd import QSPIProgramProvider, ZynqJTAGProvider


class AMDProviderTests(unittest.TestCase):
    def _profile(self, root: Path, *, stage: StageConfig, toolchains: dict[str, ToolchainConfig]) -> FlowConfig:
        return FlowConfig(
            schema_version=1,
            project_name="generic-amd-project",
            root=root,
            config_path=root / "fpga-flow.toml",
            output_dir=root / "out/fpga-flow",
            default_target="board-a",
            toolchains=MappingProxyType(toolchains),
            targets=MappingProxyType({"board-a": MappingProxyType({"family": "zynq"})}),
            stages=MappingProxyType({
                "hardware": StageConfig("hardware", "build", (), (),
                                         ("out/hardware/design.bit",), ("fake",), root, (),
                                         MappingProxyType({})),
                "platform": StageConfig("platform", "build", (), (),
                                         ("out/platform/ps-init.tcl", "out/platform/fsbl.elf"), ("fake",), root, (),
                                         MappingProxyType({})),
                "app": StageConfig("app", "build", (), (),
                                   ("out/app/application.elf",), ("fake",), root, (),
                                   MappingProxyType({})),
                "boot": StageConfig("boot", "build", (), (),
                                    ("out/boot/BOOT.BIN",), ("fake",), root, (),
                                    MappingProxyType({})),
                stage.name: stage,
            }),
            suites=MappingProxyType({}),
        )

    def _artifacts(self, root: Path) -> None:
        for relative in (
            "out/hardware/design.bit",
            "out/platform/ps-init.tcl",
            "out/app/application.elf",
            "out/boot/BOOT.BIN",
            "out/platform/fsbl.elf",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"artifact")

    def test_jtag_provider_renders_profile_owned_targets_and_executes_argv(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fpga flow jtag ") as tmp:
            root = Path(tmp)
            self._artifacts(root)
            fake = root / "fake_xsct.py"
            fake.write_text("import sys; print('XSCT_OK'); print(sys.argv[1])\n", encoding="utf-8")
            stage = StageConfig(
                "deploy_jtag", "deploy", ("hardware", "platform", "app"),
                ("out/hardware/design.bit", "out/platform/ps-init.tcl", "out/app/application.elf"),
                (), (), root, ("xsct",), MappingProxyType({}), 60,
                target="board-a", destructive=True,
                runner="amd.zynq.jtag",
                options=MappingProxyType({
                    "bitstream": "out/hardware/design.bit",
                    "ps_init_script": "out/platform/ps-init.tcl",
                    "elf": "out/app/application.elf",
                    "hw_server_url": "tcp:localhost:3121",
                    "ps_target_pattern": "PSU",
                    "pl_target_pattern": "xczu*",
                    "cpu_target_pattern": "APU*",
                    "ps_init_commands": ["psu_init", "psu_post_config"],
                    "run_ms": 20,
                }),
            )
            profile = self._profile(root, stage=stage, toolchains={
                "xsct": ToolchainConfig("xsct", (sys.executable, str(fake)), (), None),
            })
            run_dir = root / "out/fpga-flow/runs/jtag-run"
            run_dir.mkdir(parents=True)
            result = ZynqJTAGProvider().execute(stage, profile, run_dir, "board-a")
            self.assertTrue(result.succeeded, result.stderr)
            self.assertIn("XSCT_OK", result.stdout)
            script = (run_dir / "jtag_program.tcl").read_text(encoding="utf-8")
            self.assertIn("select_unique_target $pl_target_pattern PL", script)
            self.assertIn("psu_post_config", script)
            self.assertIn("PSU", script)
            self.assertIn("恰好匹配一个目标", script)

    def test_jtag_provider_preflight_does_not_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_xsct_probe.py"
            fake.write_text(
                "print('PREFLIGHT_TARGET_OK PS')\n"
                "print('PREFLIGHT_TARGET_OK PL')\n"
                "print('PREFLIGHT_TARGET_OK CPU')\n"
                "print('PREFLIGHT_READ_ONLY')\n",
                encoding="utf-8",
            )
            stage = StageConfig(
                "deploy_jtag", "deploy", (), (), (), (), root, ("xsct",),
                MappingProxyType({}), target="board-a", runner="amd.zynq.jtag",
                options=MappingProxyType({
                    "hw_server_url": "tcp:localhost:3121",
                    "ps_target_pattern": "PSU", "pl_target_pattern": "xczu*",
                    "cpu_target_pattern": "APU*",
                }),
            )
            profile = self._profile(root, stage=stage, toolchains={
                "xsct": ToolchainConfig("xsct", (sys.executable, str(fake)), (), None),
            })
            result = ZynqJTAGProvider().preflight(stage, profile, root / "preflight", "board-a")
            self.assertTrue(result["read_only"])
            self.assertEqual(len(result["targets"]), 3)
            script = (root / "preflight" / "jtag_preflight.tcl").read_text(encoding="utf-8")
            self.assertNotIn("rst -", script)
            self.assertNotIn("fpga ", script)
            self.assertNotIn("dow ", script)

    def test_qspi_provider_probes_unique_target_and_verifies_readback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fpga flow qspi ") as tmp:
            root = Path(tmp)
            self._artifacts(root)
            fake = root / "fake_program_flash.py"
            fake.write_text(
                "import sys\n"
                "if '-jtagtargets' in sys.argv:\n"
                " print('2 board-serial-1 (name arm_dap idcode 4ba00477)')\n"
                " raise SystemExit(0)\n"
                "print('Verify Operation successful.')\n"
                "print('Flash Operation Successful')\n",
                encoding="utf-8",
            )
            stage = StageConfig(
                "deploy_qspi", "deploy", ("boot", "platform"),
                ("out/boot/BOOT.BIN", "out/platform/fsbl.elf"), (), (), root,
                ("program_flash",), MappingProxyType({}), 120,
                target="board-a", destructive=True,
                runner="amd.qspi.program",
                options=MappingProxyType({
                    "boot_image": "out/boot/BOOT.BIN",
                    "fsbl": "out/platform/fsbl.elf",
                    "flash_type": "qspi-x4-single",
                    "offset": 0,
                    "target_device_name": "arm_dap",
                    "target_name_contains": "board-serial-1",
                    "server_url": "tcp:localhost:3121",
                }),
            )
            profile = self._profile(root, stage=stage, toolchains={
                "program_flash": ToolchainConfig("program_flash", (sys.executable, str(fake)), (), None),
            })
            run_dir = root / "out/fpga-flow/runs/qspi-run"
            run_dir.mkdir(parents=True)
            preflight = QSPIProgramProvider().preflight(stage, profile, run_dir / "preflight", "board-a")
            self.assertTrue(preflight["read_only"])
            self.assertEqual(preflight["target"]["target_name"], "board-serial-1")
            result = QSPIProgramProvider().execute(stage, profile, run_dir, "board-a")
            self.assertTrue(result.succeeded, result.stderr)
            self.assertIn("Verify Operation successful", result.stdout)
            self.assertIn("-blank_check", result.command)
            self.assertIn("-verify", result.command)
            self.assertNotIn("-erase_all", result.command)
            selection = (run_dir / "target-selection.json").read_text(encoding="utf-8")
            self.assertIn("board-serial-1", selection)
            self.assertFalse(json_read(selection)["erase_all"])

    def test_jtag_provider_rejects_artifacts_not_owned_by_prerequisite_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._artifacts(root)
            fake = root / "fake_xsct.py"
            fake.write_text("print('unused')\n", encoding="utf-8")
            stage = StageConfig(
                "deploy_jtag", "deploy", ("hardware", "platform", "app"), (), (), (), root,
                ("xsct",), MappingProxyType({}), target="board-a", destructive=True,
                runner="amd.zynq.jtag", options=MappingProxyType({
                    "bitstream": "out/boot/BOOT.BIN", "ps_init_script": "out/platform/ps-init.tcl",
                    "elf": "out/app/application.elf", "hw_server_url": "tcp:localhost:3121",
                    "ps_target_pattern": "APU", "pl_target_pattern": "xc7z020*",
                    "cpu_target_pattern": "CPU0", "ps_init_commands": ["ps7_init"],
                })
            )
            profile = self._profile(root, stage=stage, toolchains={
                "xsct": ToolchainConfig("xsct", (sys.executable, str(fake)), (), None),
            })
            run_dir = root / "out/fpga-flow/runs/jtag-run"
            run_dir.mkdir(parents=True)
            with self.assertRaises(ValueError):
                ZynqJTAGProvider().execute(stage, profile, run_dir, "board-a")


def json_read(value: str) -> dict:
    import json
    return json.loads(value)


if __name__ == "__main__":
    unittest.main()
