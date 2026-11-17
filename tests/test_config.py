from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

from fpga_flow.config import ProfileError, load_json_schema, load_profile, topological_order


NON_HLS_PROFILE = textwrap.dedent("""
    schema_version = 1

    [project]
    name = "rtl_ps_demo"

    [flow]
    output_dir = "out/fpga-flow"
    default_target = "zybo"

    [toolchains.vivado]
    command = "vivado"
    version_args = ["-version"]
    expected_version = "2025.2.1"

    [toolchains.vitis]
    command = ["/tools/Vitis/bin/vitis"]
    version_args = ["-version"]

    [targets.zybo]
    family = "amd-zynq-7000"
    part = "xc7z020clg400-1"
    board_part = "digilentinc.com:zybo-z7-20:part0:1.2"

    [stages.hardware]
    kind = "build"
    needs = []
    inputs = ["src/rtl/**/*.sv", "src/bd/**/*.bd", "src/constraints/**/*.xdc"]
    outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]
    command = ["python3", "scripts/build_hw.py", "--jobs", "4"]
    toolchains = ["vivado"]
    timeout_seconds = 7200

    [stages.platform]
    kind = "build"
    needs = ["hardware"]
    inputs = ["src/ps/platform/**"]
    outputs = ["out/ps/platform.xpfm", "out/ps/fsbl.elf"]
    command = ["vitis", "-s", "scripts/build_ps.py", "--stage", "platform"]
    toolchains = ["vitis"]

    [stages.app]
    kind = "build"
    needs = ["platform"]
    inputs = ["src/ps/app/**"]
    outputs = ["out/ps/app.elf"]
    command = ["vitis", "-s", "scripts/build_ps.py", "--stage", "app"]
    toolchains = ["vitis"]

    [stages.board_smoke]
    kind = "validate"
    needs = ["app"]
    inputs = []
    outputs = ["out/board/smoke.json"]
    command = ["python3", "scripts/board_smoke.py"]
    target = "zybo"

    [suites]
    smoke = ["board_smoke"]
""")


HLS_PROFILE = textwrap.dedent("""
    schema_version = 1

    [project]
    name = "hls_ps_demo"

    [toolchains.vivado]
    command = "/tools/AMD/Vivado/bin/vivado"

    [toolchains.vitis]
    command = "/tools/AMD/Vitis/bin/vitis"

    [targets.zybo]
    family = "amd-zynq-7000"
    part = "xc7z020clg400-1"

    [stages.hls_csim]
    kind = "test"
    command = ["vitis-run", "--csim", "--config", "hls.cfg"]
    inputs = ["hls/src/**/*.cpp", "hls/cfg/**/*.cfg"]
    outputs = ["out/hls/csim.log"]
    toolchains = ["vitis"]

    [stages.hls_synth]
    kind = "build"
    needs = ["hls_csim"]
    command = ["vitis-run", "--csynth", "--config", "hls.cfg"]
    inputs = ["hls/src/**/*.cpp", "hls/cfg/**/*.cfg"]
    outputs = ["out/hls/ip/component.xml"]
    toolchains = ["vitis"]

    [stages.hardware]
    kind = "build"
    needs = ["hls_synth"]
    command = ["vivado", "-mode", "batch", "-source", "scripts/build.tcl"]
    inputs = ["vivado/**/*.tcl", "vivado/**/*.xdc"]
    outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]
    toolchains = ["vivado"]

    [stages.platform]
    kind = "build"
    needs = ["hardware"]
    command = ["vitis", "-s", "scripts/build_ps.py", "--stage", "platform"]
    outputs = ["out/ps/platform.xpfm"]
    toolchains = ["vitis"]

    [stages.app]
    kind = "build"
    needs = ["platform"]
    command = ["vitis", "-s", "scripts/build_ps.py", "--stage", "app"]
    outputs = ["out/ps/app.elf"]
    toolchains = ["vitis"]

    [stages.boot]
    kind = "build"
    needs = ["app"]
    command = ["bootgen", "-arch", "zynq", "-image", "boot.bif", "-o", "out/boot.bin"]
    outputs = ["out/boot.bin"]
    toolchains = ["vitis"]

    [suites]
    hls = ["hls_csim"]
""")


class ConfigTests(unittest.TestCase):
    def _load_text(self, source: str):
        temporary = tempfile.TemporaryDirectory(prefix="FPGA Flow Workspace ")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "fpga-flow.toml"
        path.write_text(source, encoding="utf-8")
        return load_profile(path)

    def test_non_hls_profile_loads_and_orders_dependencies(self) -> None:
        profile = self._load_text(NON_HLS_PROFILE)
        self.assertEqual(profile.project_name, "rtl_ps_demo")
        self.assertEqual(profile.default_target, "zybo")
        self.assertNotIn("hls_csim", profile.stages)
        self.assertEqual(topological_order(profile, ["app"]), ["hardware", "platform", "app"])
        self.assertEqual(profile.stages["hardware"].command[:2], ("python3", "scripts/build_hw.py"))
        self.assertEqual(profile.stages["board_smoke"].cwd, profile.root)

    def test_optional_parallelism_resources_and_side_effects_are_project_profile_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = NON_HLS_PROFILE.replace(
                "[flow]",
                "[flow]\nmax_parallel_stages = 2",
                1,
            ).replace(
                "[stages.hardware]",
                '[stages.hardware]\nresources = ["shared-vivado-workspace"]\nside_effects = ["persistent-storage"]',
                1,
            )
            config = root / "fpga-flow.toml"
            config.write_text(source, encoding="utf-8")
            profile = load_profile(config)
            self.assertEqual(profile.max_parallel_stages, 2)
            self.assertEqual(profile.stages["hardware"].resources, ("shared-vivado-workspace",))
            self.assertEqual(profile.stages["hardware"].side_effects, ("persistent-storage",))
            default_profile = root / "default.toml"
            default_profile.write_text(NON_HLS_PROFILE, encoding="utf-8")
            self.assertEqual(load_profile(default_profile).max_parallel_stages, 1)

    def test_optional_hls_stages_are_supported_without_special_cases(self) -> None:
        profile = self._load_text(HLS_PROFILE)
        self.assertEqual(
            topological_order(profile, ["boot"]),
            ["hls_csim", "hls_synth", "hardware", "platform", "app", "boot"],
        )
        self.assertEqual(profile.stages["hls_csim"].kind, "test")

    def test_project_relative_posix_paths_resolve_safely_with_spaces(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'output_dir = "out/fpga-flow"',
            'output_dir = "out with spaces/fpga-flow"',
            1,
        )
        source = source.replace(
            'inputs = ["src/rtl/**/*.sv", "src/bd/**/*.bd", "src/constraints/**/*.xdc"]',
            'inputs = ["src/RTL Files/**/*.sv", "src/bd/**/*.bd", "src/constraints/**/*.xdc"]',
            1,
        )
        source = source.replace(
            'outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]',
            'outputs = ["out/hardware/design top.bit", "out/hardware/design.xsa"]',
            1,
        )
        source = source.replace(
            'command = ["python3", "scripts/build_hw.py", "--jobs", "4"]',
            'command = ["C:/Program Files/AMD Vivado/bin/vivado.bat", "-mode", "batch"]\ncwd = "tools/build scripts"',
            1,
        )
        profile = self._load_text(source)
        self.assertEqual(profile.output_dir, profile.root / "out with spaces/fpga-flow")
        self.assertEqual(
            profile.stages["hardware"].inputs[0],
            "src/RTL Files/**/*.sv",
        )
        self.assertEqual(
            profile.stages["hardware"].outputs[0],
            "out/hardware/design top.bit",
        )
        self.assertEqual(profile.stages["hardware"].cwd, profile.root / "tools/build scripts")
        self.assertTrue(profile.stages["hardware"].cwd.is_absolute())
        self.assertEqual(profile.stages["hardware"].cwd.relative_to(profile.root).as_posix(),
                         "tools/build scripts")

    def test_executable_paths_with_spaces_are_preserved_as_argv(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = "vivado"',
            'command = ["C:/Program Files/AMD/Vivado Suite/bin/vivado.bat"]',
            1,
        )
        source = source.replace(
            'command = ["python3", "scripts/build_hw.py", "--jobs", "4"]',
            'command = ["C:/Program Files/AMD/Vitis Suite/bin/vitis.bat", "-s", "build flow.py"]',
            1,
        )
        profile = self._load_text(source)
        self.assertEqual(
            profile.toolchains["vivado"].command,
            ("C:/Program Files/AMD/Vivado Suite/bin/vivado.bat",),
        )
        self.assertEqual(
            profile.stages["hardware"].command,
            ("C:/Program Files/AMD/Vitis Suite/bin/vitis.bat", "-s", "build flow.py"),
        )

    @unittest.skipUnless(os.name == "nt", "需要 Windows 原生路径语义")
    def test_profile_loads_from_native_windows_location_with_spaces(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="FPGA Flow Windows ")
        self.addCleanup(temporary.cleanup)
        project_root = Path(temporary.name) / "project root with spaces"
        project_root.mkdir()
        path = project_root / "fpga-flow.toml"
        path.write_text(NON_HLS_PROFILE, encoding="utf-8")
        profile = load_profile(path)
        self.assertEqual(profile.root, project_root.resolve())
        self.assertEqual(profile.output_dir, profile.root / "out/fpga-flow")
        self.assertEqual(profile.config_path, path.resolve())

    def test_output_glob_is_preserved_as_project_relative_artifact_pattern(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]',
            'outputs = ["out/hardware/design-*.bit", "out/**/design.xsa"]',
            1,
        )
        profile = self._load_text(source)
        self.assertEqual(
            profile.stages["hardware"].outputs,
            ("out/hardware/design-*.bit", "out/**/design.xsa"),
        )

    def test_output_glob_rejects_existing_symlink_match(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="FPGA Flow Output Glob ")
        self.addCleanup(temporary.cleanup)
        project_root = Path(temporary.name) / "project"
        outside_root = Path(temporary.name) / "outside"
        (project_root / "out" / "hardware").mkdir(parents=True)
        outside_root.mkdir()
        external_artifact = outside_root / "design.xsa"
        external_artifact.write_bytes(b"external")
        link = project_root / "out" / "hardware" / "linked.xsa"
        try:
            link.symlink_to(external_artifact)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        source = NON_HLS_PROFILE.replace(
            'outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]',
            'outputs = ["out/**/*.xsa"]',
            1,
        )
        profile_path = project_root / "fpga-flow.toml"
        profile_path.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ProfileError, "符号链接"):
            load_profile(profile_path)

    def test_profile_rejects_symlink_components_before_resolving(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="FPGA Flow Symlink ")
        self.addCleanup(temporary.cleanup)
        project_root = Path(temporary.name) / "project"
        outside_root = Path(temporary.name) / "outside"
        project_root.mkdir()
        outside_root.mkdir()
        (project_root / "src").mkdir()
        link = project_root / "src" / "linked"
        try:
            link.symlink_to(outside_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        source = NON_HLS_PROFILE.replace(
            'inputs = ["src/rtl/**/*.sv", "src/bd/**/*.bd", "src/constraints/**/*.xdc"]',
            'inputs = ["src/linked/**/*.sv"]',
            1,
        )
        path = project_root / "fpga-flow.toml"
        path.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ProfileError, "符号链接"):
            load_profile(path)

        (project_root / "out").mkdir()
        output_link = project_root / "out" / "external"
        try:
            output_link.symlink_to(outside_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        source = NON_HLS_PROFILE.replace(
            'outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]',
            'outputs = ["out/external/design.bit"]',
            1,
        )
        path.write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(ProfileError, "符号链接"):
            load_profile(path)

    def test_builtin_runner_has_options_without_command(self) -> None:
        source = NON_HLS_PROFILE.replace(
            '[stages.board_smoke]\nkind = "validate"',
            '[stages.board_smoke]\nkind = "deploy"\nrunner = "amd.qspi.program"\noptions = { flash_type = "qspi-x4-single", offset = 0, erase_all = false }',
            1,
        )
        source = source.replace('command = ["python3", "scripts/board_smoke.py"]\n', "", 1)
        source = source.replace('smoke = ["board_smoke"]', 'smoke = []', 1)
        profile = self._load_text(source)
        stage = profile.stages["board_smoke"]
        self.assertEqual(stage.runner, "amd.qspi.program")
        self.assertEqual(stage.command, ())
        self.assertEqual(stage.options["flash_type"], "qspi-x4-single")
        self.assertIs(stage.options["erase_all"], False)

    def test_cycles_and_unknown_stage_references_fail_closed(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'needs = []\ninputs = ["src/rtl',
            'needs = ["app"]\ninputs = ["src/rtl',
            1,
        )
        with self.assertRaisesRegex(ProfileError, "循环依赖"):
            self._load_text(source)

        source = NON_HLS_PROFILE.replace('needs = ["hardware"]', 'needs = ["missing"]', 1)
        with self.assertRaisesRegex(ProfileError, "未知阶段"):
            self._load_text(source)

        source = NON_HLS_PROFILE.replace('smoke = ["board_smoke"]', 'smoke = ["missing"]', 1)
        with self.assertRaisesRegex(ProfileError, "未知阶段"):
            self._load_text(source)

    def test_uart_commands_provider_accepts_nested_steps_without_command(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'kind = "validate"',
            'kind = "validate"\nrunner = "amd.uart.commands"',
            1,
        )
        source = source.replace(
            'command = ["python3", "scripts/board_smoke.py"]\n',
            "",
            1,
        )
        source = source.replace(
            '[suites]\n',
            '[[stages.board_smoke.options.steps]]\n'
            'command = ["status"]\n'
            'expect = "frame="\n'
            'timeout_seconds = 3\n'
            '\n'
            '[[stages.board_smoke.options.steps]]\n'
            'command = ["rom status"]\n'
            'expect = "rom loaded=1"\n'
            '\n'
            '[suites]\n',
            1,
        )
        profile = self._load_text(source)
        stage = profile.stages["board_smoke"]
        self.assertEqual(stage.runner, "amd.uart.commands")
        self.assertEqual(stage.command, ())
        self.assertEqual(stage.options["steps"], [
            {"command": ["status"], "expect": "frame=", "timeout_seconds": 3},
            {"command": ["rom status"], "expect": "rom loaded=1"},
        ])

    def test_version_command_is_separate_argv_from_tool_execution(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = ["/tools/Vitis/bin/vitis"]',
            'command = ["/tools/Vitis/bin/program_flash"]\n'
            'version_command = ["/tools/Vitis/bin/vitis", "-version"]',
            1,
        )
        profile = self._load_text(source)
        program_flash = profile.toolchains["vitis"]
        self.assertEqual(program_flash.command, ("/tools/Vitis/bin/program_flash",))
        self.assertEqual(program_flash.version_command, ("/tools/Vitis/bin/vitis", "-version"))

    def test_version_command_preserves_windows_paths_with_spaces(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = "vivado"',
            'command = ["C:/Program Files/AMD/Vivado/bin/program_flash.bat"]\n'
            'version_command = ["C:/Program Files/AMD/Vitis/bin/vitis.bat", "-version"]',
            1,
        )
        profile = self._load_text(source)
        self.assertEqual(
            profile.toolchains["vivado"].command,
            ("C:/Program Files/AMD/Vivado/bin/program_flash.bat",),
        )
        self.assertEqual(
            profile.toolchains["vivado"].version_command,
            ("C:/Program Files/AMD/Vitis/bin/vitis.bat", "-version"),
        )

    def test_version_command_must_be_nonempty_argv(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = "vivado"',
            'command = "vivado"\nversion_command = "vitis -version"',
            1,
        )
        with self.assertRaisesRegex(ProfileError, "字符串数组"):
            self._load_text(source)

    def test_unknown_toolchain_and_target_references_fail(self) -> None:
        source = NON_HLS_PROFILE.replace('toolchains = ["vivado"]', 'toolchains = ["missing"]', 1)
        with self.assertRaisesRegex(ProfileError, "未知工具链"):
            self._load_text(source)

        source = NON_HLS_PROFILE.replace('target = "zybo"', 'target = "missing"', 1)
        with self.assertRaisesRegex(ProfileError, "未知目标"):
            self._load_text(source)

    def test_paths_cannot_escape_project_root(self) -> None:
        bad_paths = ("../outside.bit", "/tmp/outside.bit", "C:/outside.bit", r"src\outside.bit", "out/**/../outside.bit")
        for bad_path in bad_paths:
            with self.subTest(path=bad_path):
                source = NON_HLS_PROFILE.replace(
                    'outputs = ["out/hardware/design.bit", "out/hardware/design.xsa"]',
                    f'outputs = ["{bad_path}"]',
                    1,
                )
                with self.assertRaises(ProfileError):
                    self._load_text(source)

        source = NON_HLS_PROFILE.replace('output_dir = "out/fpga-flow"', 'output_dir = "../outside"')
        with self.assertRaises(ProfileError):
            self._load_text(source)

        source = NON_HLS_PROFILE.replace('cwd = "."', 'cwd = "../outside"')
        # 基础配置使用默认 cwd；此处显式加入路径以检查安全解析。
        source = source.replace('command = ["python3", "scripts/build_hw.py", "--jobs", "4"]',
                                'command = ["python3", "scripts/build_hw.py", "--jobs", "4"]\ncwd = "../outside"', 1)
        with self.assertRaises(ProfileError):
            self._load_text(source)

    def test_command_runner_requires_a_nonempty_command(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = ["python3", "scripts/build_hw.py", "--jobs", "4"]\n',
            "",
            1,
        )
        with self.assertRaisesRegex(ProfileError, "command"):
            self._load_text(source)

    def test_shell_command_strings_are_rejected(self) -> None:
        source = NON_HLS_PROFILE.replace(
            'command = ["python3", "scripts/build_hw.py", "--jobs", "4"]',
            'command = "python3 scripts/build_hw.py"',
            1,
        )
        with self.assertRaisesRegex(ProfileError, "argv 数组"):
            self._load_text(source)

    def test_unknown_profile_keys_are_rejected(self) -> None:
        source = NON_HLS_PROFILE.replace("[project]", "[project]\nunknown = true", 1)
        with self.assertRaisesRegex(ProfileError, "不支持的键"):
            self._load_text(source)

    def test_json_schema_is_packaged_and_rejects_native_absolute_paths(self) -> None:
        schema = load_json_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        pattern = schema["$defs"]["projectRelativePath"]["pattern"]
        for unsafe in ("/absolute/path", "C:/absolute/path", r"src\outside", "../outside", "src/../outside"):
            with self.subTest(path=unsafe):
                self.assertIsNone(re.match(pattern, unsafe))
        for safe in ("src/rtl/**/*.sv", "out/build/design with spaces.bit"):
            with self.subTest(path=safe):
                self.assertIsNotNone(re.match(pattern, safe))

    def test_installed_wheel_can_load_packaged_schema(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="fpga-flow-wheel-") as temporary:
            temp_root = Path(temporary)
            build_source = temp_root / "source"
            shutil.copytree(
                repository,
                build_source,
                ignore=shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__", "out"),
            )
            wheel_dir = temp_root / "wheel"
            install_dir = temp_root / "installed"
            wheel_dir.mkdir()
            wheel_command = [
                sys.executable, "-m", "pip", "wheel",
                "--disable-pip-version-check", "--no-deps",
            ]
            # 若已安装满足版本要求的构建后端，则关闭隔离构建以避免联网。
            try:
                setuptools_version = importlib.metadata.version("setuptools")
            except importlib.metadata.PackageNotFoundError:
                setuptools_version = ""
            match = re.match(r"^(\d+)\.(\d+)", setuptools_version)
            if match and tuple(map(int, match.groups())) >= (75, 0):
                wheel_command.append("--no-build-isolation")
            wheel_command.extend(["--wheel-dir", str(wheel_dir), str(build_source)])
            subprocess.run(
                wheel_command,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            wheels = list(wheel_dir.glob("fpga_flow-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                self.assertIn("fpga_flow/schema/flow-v1.schema.json", archive.namelist())
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-deps", "--target",
                    str(install_dir), str(wheels[0]),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(install_dir)
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    "from fpga_flow.config import load_json_schema; "
                    "assert load_json_schema()['properties']['schema_version']['const'] == 1",
                ],
                cwd=temp_root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
