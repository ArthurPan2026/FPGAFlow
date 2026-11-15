from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fpga_flow.amd_deploy import (
    DeployCommandError,
    build_qspi_program_argv,
    parse_program_flash_targets,
    program_flash_query_argv,
    render_zynq_jtag_program_tcl,
    select_unique_flash_target,
)


class AmdDeployHelpersTests(unittest.TestCase):
    def test_program_flash_targets_parse_and_profile_selects_exactly_one(self) -> None:
        targets = parse_program_flash_targets(
            "2 jsn-Board A-4ba00477-0 (name arm_dap idcode 4ba00477)\n"
            "3 jsn-Board A-23727093-0 (name xc7z020 idcode 23727093)\n"
        )
        selected = select_unique_flash_target(targets, device_name="arm_dap", target_name_contains="Board A")
        self.assertEqual(selected["id"], "2")
        self.assertEqual(selected["idcode"], "4ba00477")
        with self.assertRaises(DeployCommandError):
            select_unique_flash_target(targets, device_name="missing")

    def test_target_query_argv_preserves_server_url_as_one_argument(self) -> None:
        argv = program_flash_query_argv("C:/AMD Tools/Vitis/program_flash.bat", "tcp:127.0.0.1:3121")
        self.assertEqual(argv[0], "C:/AMD Tools/Vitis/program_flash.bat")
        self.assertEqual(argv[-1], "tcp:127.0.0.1:3121")

    def test_qspi_command_requires_readback_and_never_erases_all(self) -> None:
        argv = build_qspi_program_argv(
            "program_flash", "out/BOOT.BIN", "out/fsbl.elf",
            flash_type="qspi-x4-single", offset=0x400000,
            target_name="board serial 123", server_url="tcp:localhost:3121",
        )
        self.assertIn("-blank_check", argv)
        self.assertIn("-verify", argv)
        self.assertNotIn("-erase_all", argv)
        self.assertEqual(argv[argv.index("-offset") + 1], str(0x400000))
        self.assertEqual(argv[argv.index("-target_name") + 1], "board serial 123")
        with self.assertRaises(DeployCommandError):
            build_qspi_program_argv("p", "b", "f", flash_type="qspi", offset=-1,
                                    target_name="board", server_url="tcp:localhost:3121")

    def test_jtag_script_is_profile_parameterized_and_quotes_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("design image.bit", "ps init.tcl", "app image.elf"):
                (root / filename).write_bytes(b"x")
            script = render_zynq_jtag_program_tcl(
                root / "design image.bit", root / "ps init.tcl", root / "app image.elf",
                hw_server_url="tcp:localhost:3121", run_ms=250,
                ps_target_pattern="PSU", pl_target_pattern="xczu*",
                cpu_target_pattern="APU*", ps_init_commands=("psu_init", "psu_post_config"),
            )
            self.assertIn("select_unique_target $pl_target_pattern PL", script)
            self.assertIn('set ps_target_pattern "PSU"', script)
            self.assertIn("psu_post_config", script)
            self.assertIn("APP_ELF_DOWNLOADED", script)
            self.assertIn("恰好匹配一个目标", script)
            self.assertIn("design image.bit", script)
        with self.assertRaises(DeployCommandError):
            render_zynq_jtag_program_tcl("missing.bit", "missing.tcl", "missing.elf",
                                         hw_server_url="tcp:localhost:3121")


if __name__ == "__main__":
    unittest.main()
