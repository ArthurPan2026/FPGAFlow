from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from fpga_flow.cli import main
from fpga_flow.config import load_profile
from fpga_flow.engine import FlowEngine, FlowError
from fpga_flow.manifest import ManifestStore


FAKE_STAGE = '''from pathlib import Path
import sys

mode = sys.argv[1]
out = Path(sys.argv[2])
out.parent.mkdir(parents=True, exist_ok=True)
if mode == "fail":
    print("intentional-failure", file=sys.stderr)
    raise SystemExit(23)
if mode == "target":
    out.write_text(sys.argv[3], encoding="utf-8")
    raise SystemExit(0)
if mode == "count":
    counter = Path(sys.argv[3])
    counter.parent.mkdir(parents=True, exist_ok=True)
    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(count + 1), encoding="utf-8")
    out.write_text(f"artifact-{count + 1}", encoding="utf-8")
    raise SystemExit(0)
out.write_text("artifact", encoding="utf-8")
'''


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML fixture value: {value!r}")


def _write_profile(
    root: Path,
    stages: Mapping[str, Mapping[str, Any]],
    *,
    suites: Mapping[str, list[str]] | None = None,
    targets: Mapping[str, Mapping[str, Any]] | None = None,
    expected_python: str | None = "Python",
) -> Path:
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "fake_stage.py").write_text(FAKE_STAGE, encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "design.txt").write_text("baseline", encoding="utf-8")
    if targets is None:
        targets = {"zybo": {"family": "amd-zynq-7000", "part": "xc7z020clg400-1"}}
    lines = [
        "schema_version = 1",
        "",
        "[project]",
        'name = "fixture-amd-project"',
        "",
        "[flow]",
        'output_dir = "out/fpga-flow"',
        'default_target = "zybo"' if targets and "zybo" in targets else "",
    ]
    if expected_python is not None:
        lines += ["", "[toolchains.python]", f"command = [{_quote(sys.executable)}]",
                  'version_args = ["--version"]', f"expected_version = {_quote(expected_python)}"]
    for target_name, target_values in (targets or {}).items():
        lines += ["", f"[targets.{target_name}]"]
        for key, value in target_values.items():
            if not isinstance(value, Mapping):
                lines.append(f"{key} = {_toml_value(value)}")
        for key, value in target_values.items():
            if isinstance(value, Mapping):
                lines += ["", f"[targets.{target_name}.{key}]"]
                for nested_key, nested_value in value.items():
                    lines.append(f"{nested_key} = {_toml_value(nested_value)}")
    for name, stage in stages.items():
        lines += ["", f"[stages.{name}]", f"kind = {_quote(stage['kind'])}"]
        lines.append("needs = [" + ", ".join(_quote(item) for item in stage.get("needs", [])) + "]")
        lines.append("inputs = [" + ", ".join(_quote(item) for item in stage.get("inputs", ["src/**/*.txt", "tools/fake_stage.py"])) + "]")
        lines.append("outputs = [" + ", ".join(_quote(item) for item in stage.get("outputs", [])) + "]")
        if "command" in stage:
            lines.append("command = [" + ", ".join(_quote(item) for item in stage["command"]) + "]")
        if "runner" in stage:
            lines.append(f"runner = {_quote(stage['runner'])}")
        if "target" in stage:
            lines.append(f"target = {_quote(stage['target'])}")
        if "destructive" in stage:
            lines.append(f"destructive = {'true' if stage['destructive'] else 'false'}")
        if stage.get("toolchains", expected_python is not None):
            toolchains = stage.get("toolchains", ["python"] if expected_python is not None else [])
            lines.append("toolchains = [" + ", ".join(_quote(item) for item in toolchains) + "]")
        if "success_markers" in stage:
            lines.append("success_markers = [" + ", ".join(_quote(item) for item in stage["success_markers"]) + "]")
        if "failure_markers" in stage:
            lines.append("failure_markers = [" + ", ".join(_quote(item) for item in stage["failure_markers"]) + "]")
        if stage.get("options"):
            lines += ["", f"[stages.{name}.options]"]
            for option_name, option_value in stage["options"].items():
                lines.append(f"{option_name} = {_toml_value(option_value)}")
    for suite, names in (suites or {}).items():
        lines += ["", f"[suites]"] if "[suites]" not in lines else []
        lines.append(f"{suite} = [" + ", ".join(_quote(item) for item in names) + "]")
    profile = root / "fpga-flow.toml"
    profile.write_text("\n".join(line for line in lines if line is not None) + "\n", encoding="utf-8")
    return profile


def _count_stage(output: str, counter: str = "out/counter.txt", *, needs: list[str] | None = None) -> dict[str, Any]:
    return {
        "kind": "build",
        "needs": needs or [],
        "command": [sys.executable, "tools/fake_stage.py", "count", output, counter],
        "outputs": [output, counter],
    }


class FakeFlashProvider:
    def execute(self, stage: Any, profile: Any, run_dir: Path, target: str | None):
        from fpga_flow.runner import run_command

        return run_command(profile.toolchains["program_flash"].command, cwd=profile.root,
                           env=stage.env, timeout_seconds=stage.timeout_seconds)


class FakeTransportProvider:
    def execute(self, stage: Any, profile: Any, run_dir: Path, target: str | None):
        from fpga_flow.runner import run_command

        return run_command(
            [sys.executable, "tools/fake_stage.py", "target", stage.options["receipt"], target or ""],
            cwd=profile.root,
            env=stage.env,
            timeout_seconds=stage.timeout_seconds,
        )


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _engine(self, stages: Mapping[str, Mapping[str, Any]], **kwargs: Any) -> FlowEngine:
        profile = _write_profile(self.root, stages, **kwargs)
        return FlowEngine(load_profile(profile))

    def test_release_does_not_run_board_checks_for_an_unselected_deployment(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit"),
            "deploy_jtag": {"kind": "deploy", "needs": ["hardware"], "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/jtag", "{target}"],
                "outputs": ["out/jtag"]},
            "runtime": {"kind": "validate", "needs": ["deploy_jtag"], "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/runtime", "{target}"],
                "outputs": ["out/runtime"]},
        }
        engine = self._engine(stages)
        result = engine.release(target="zybo")
        self.assertEqual([item["stage"] for item in result["stages"]], ["hardware"])
        self.assertFalse((self.root / "out/jtag").exists())
        self.assertFalse((self.root / "out/runtime").exists())

    def test_runtime_evidence_is_not_reused_across_api_operations(self) -> None:
        stages = {
            "health": {"kind": "validate", "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/health", "{target}"],
                "outputs": ["out/health"]},
            "audio": {"kind": "validate", "needs": ["health"], "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/audio", "{target}"],
                "outputs": ["out/audio"]},
        }
        engine = self._engine(stages, suites={"health": ["health"], "audio": ["audio"]})
        engine.validate("health")
        with self.assertRaises(FlowError) as caught:
            engine.validate("audio")
        self.assertEqual(caught.exception.code, "FLOW-DEPENDENCY-STALE")
        self.assertFalse((self.root / "out/audio").exists())

    def test_plan_is_read_only_and_launches_no_stage_or_toolchain_commands(self) -> None:
        engine = self._engine({"hardware": _count_stage("out/hardware.bit")})
        result = engine.plan(target="zybo")
        self.assertFalse(result["commands_launched"])
        self.assertEqual(result["stages"][0]["action"], "run")
        self.assertFalse((self.root / "out/fpga-flow").exists())
        self.assertFalse((self.root / "out/counter.txt").exists())

    def test_build_cache_hit_rehashes_outputs_and_does_not_rerun_command(self) -> None:
        engine = self._engine({"hardware": _count_stage("out/hardware.bit")})
        first = engine.build("hardware")
        first_run_id = first["stages"][0]["run_id"]
        first_run_dir = engine.store.run_path(first_run_id).parent
        self.assertEqual(json.loads((first_run_dir / "run.json").read_text(encoding="utf-8"))["run_id"], first_run_id)
        self.assertEqual(engine.store.read_run_record(first_run_id)["fpga_flow_version"], "0.3.0")
        self.assertEqual(json.loads((first_run_dir / "command.json").read_text(encoding="utf-8"))["argv"][0], sys.executable)
        self.assertEqual((self.root / "out/counter.txt").read_text(encoding="utf-8"), "1")
        second = engine.build("hardware")
        self.assertEqual((self.root / "out/counter.txt").read_text(encoding="utf-8"), "1")
        self.assertTrue(second["stages"][0]["cache_hit"])
        self.assertNotEqual(first["stages"][0]["run_id"], second["stages"][0]["run_id"])
        self.assertEqual(engine.status()["stages"][0]["last_evidence"], "verified")

    def test_input_content_change_invalidates_stage_cache(self) -> None:
        engine = self._engine({"hardware": _count_stage("out/hardware.bit")})
        engine.build("hardware")
        (self.root / "src/design.txt").write_text("changed", encoding="utf-8")
        result = engine.build("hardware")
        self.assertFalse(result["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/counter.txt").read_text(encoding="utf-8"), "2")

    def test_dependency_gate_blocks_without_deps_and_runs_ordered_deps_when_requested(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt"),
            "app": _count_stage("out/app.elf", "out/app-count.txt", needs=["hardware"]),
        }
        engine = self._engine(stages)
        with self.assertRaises(FlowError) as raised:
            engine.build("app")
        self.assertEqual(raised.exception.code, "FLOW-DEPENDENCY-STALE")
        self.assertFalse((self.root / "out/hardware-count.txt").exists())
        result = engine.build("app", with_deps=True)
        self.assertEqual([item["stage"] for item in result["stages"]], ["hardware", "app"])
        self.assertTrue((self.root / "out/hardware.bit").is_file())
        self.assertTrue((self.root / "out/app.elf").is_file())

    def test_failed_command_is_not_cached_and_success_history_is_preserved(self) -> None:
        stage = _count_stage("out/hardware.bit", "out/counter.txt")
        stage["command"] = [sys.executable, "tools/fake_stage.py", "fail", "out/hardware.bit", "out/counter.txt"]
        profile = _write_profile(self.root, {"hardware": stage})
        engine = FlowEngine(load_profile(profile))
        with self.assertRaises(FlowError) as raised:
            engine.build("hardware")
        self.assertEqual(raised.exception.code, "FLOW-COMMAND-FAILED")
        after_failure = engine.store.load()
        failed_id = after_failure["stages"]["hardware"]["run_id"]
        self.assertEqual(engine.store.read_run_record(failed_id)["status"], "failed")
        self.assertEqual(engine.resume()["status"], "needs-attention")

        stage["command"] = [sys.executable, "tools/fake_stage.py", "count", "out/hardware.bit", "out/counter.txt"]
        profile = _write_profile(self.root, {"hardware": stage})
        engine = FlowEngine(load_profile(profile))
        engine.build("hardware")
        after_success = engine.store.load()
        self.assertEqual(after_success["stages"]["hardware"]["status"], "succeeded")
        self.assertIn(failed_id, after_success["run_ids"])
        self.assertEqual(engine.store.read_run_record(failed_id)["status"], "failed")
        self.assertEqual((self.root / "out/counter.txt").read_text(encoding="utf-8"), "1")

    def test_toolchain_version_command_can_probe_a_sibling_tool(self) -> None:
        stage = _count_stage("out/hardware.bit")
        stage["toolchains"] = ["program_flash"]
        profile_path = _write_profile(self.root, {"hardware": stage}, expected_python=None)
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text += (
            "\n[toolchains.program_flash]\n"
            "command = [\"this-program-flash-command-is-not-run\"]\n"
            f"version_command = [{_quote(sys.executable)}, \"--version\"]\n"
            "expected_version = \"Python\"\n"
        )
        profile_path.write_text(profile_text, encoding="utf-8")
        engine = FlowEngine(load_profile(profile_path))
        result = engine.build("hardware")
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue((self.root / "out/hardware.bit").is_file())

    def test_toolchain_expected_version_lock_fails_closed(self) -> None:
        profile = _write_profile(self.root, {"hardware": _count_stage("out/hardware.bit")}, expected_python="not-a-real-python-version")
        engine = FlowEngine(load_profile(profile))
        doctor = engine.doctor()
        self.assertEqual(doctor["status"], "failed")
        with self.assertRaises(FlowError) as raised:
            engine.build("hardware")
        self.assertEqual(raised.exception.code, "FLOW-TOOLCHAIN")
        self.assertFalse((self.root / "out/hardware.bit").exists())

    def test_validate_runs_every_invocation_even_when_fingerprint_matches(self) -> None:
        stage = _count_stage("out/validation.txt", "out/validation-count.txt")
        stage["kind"] = "validate"
        profile = _write_profile(self.root, {"board_smoke": stage}, suites={"board": ["board_smoke"]})
        engine = FlowEngine(load_profile(profile))
        first = engine.validate("board")
        second = engine.validate("board")
        self.assertFalse(first["stages"][0]["cache_hit"])
        self.assertFalse(second["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/validation-count.txt").read_text(encoding="utf-8"), "2")
        plan_row = engine.plan()["stages"][0]
        self.assertEqual(plan_row["action"], "validate")
        self.assertEqual(plan_row["status"], "explicit-only")

    def test_deploy_target_filter_and_destructive_ack(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt"),
            "deploy_zybo": {
                "kind": "deploy", "needs": ["hardware"], "target": "zybo", "destructive": True,
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/zybo-programmed.txt", "{target}"],
                "outputs": ["out/zybo-programmed.txt"], "toolchains": ["python"],
            },
            "deploy_other": {
                "kind": "deploy", "target": "other", "command": [sys.executable, "tools/fake_stage.py", "target", "out/other-programmed.txt", "{target}"],
                "outputs": ["out/other-programmed.txt"], "toolchains": ["python"],
            },
        }
        targets = {"zybo": {"family": "amd-zynq-7000"}, "other": {"family": "amd-zynq-7000"}}
        engine = self._engine(stages, targets=targets)
        engine.build("hardware")
        with self.assertRaises(FlowError) as raised:
            engine.deploy("zybo")
        self.assertEqual(raised.exception.code, "FLOW-DESTRUCTIVE-ACK")
        self.assertFalse((self.root / "out/zybo-programmed.txt").exists())
        result = engine.deploy("zybo", allow_destructive=True)
        self.assertEqual([item["stage"] for item in result["stages"]], ["deploy_zybo"])
        self.assertEqual((self.root / "out/zybo-programmed.txt").read_text(encoding="utf-8"), "zybo")
        self.assertFalse((self.root / "out/other-programmed.txt").exists())

    def test_multiple_transports_on_one_target_require_explicit_deploy_stage(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt"),
            "deploy_jtag": {
                "kind": "deploy", "needs": ["hardware"], "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/jtag.receipt", "{target}"],
                "outputs": ["out/jtag.receipt"], "toolchains": ["python"],
            },
            "deploy_qspi": {
                "kind": "deploy", "needs": ["hardware"], "target": "zybo", "destructive": True,
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/qspi.receipt", "{target}"],
                "outputs": ["out/qspi.receipt"], "toolchains": ["python"],
            },
        }
        engine = self._engine(stages)
        engine.build("hardware")
        with self.assertRaises(FlowError) as raised:
            engine.deploy("zybo")
        self.assertEqual(raised.exception.code, "FLOW-DEPLOY-AMBIGUOUS")
        self.assertFalse((self.root / "out/jtag.receipt").exists())
        self.assertFalse((self.root / "out/qspi.receipt").exists())

        jtag = engine.deploy("zybo", stage_name="deploy_jtag")
        self.assertEqual([item["stage"] for item in jtag["stages"]], ["deploy_jtag"])
        self.assertTrue((self.root / "out/jtag.receipt").exists())
        self.assertFalse((self.root / "out/qspi.receipt").exists())
        with self.assertRaises(FlowError) as raised:
            engine.release(target="zybo", deploy=True, allow_destructive=True)
        self.assertEqual(raised.exception.code, "FLOW-DEPLOY-AMBIGUOUS")
        with self.assertRaises(FlowError) as raised:
            engine.release(target="zybo", deploy_stage="deploy_qspi")
        self.assertEqual(raised.exception.code, "FLOW-RELEASE-DEPLOY-OPTIN")
        qspi = engine.release(target="zybo", deploy=True, deploy_stage="deploy_qspi", allow_destructive=True)
        self.assertTrue(qspi["deploy_requested"])
        self.assertEqual(qspi["deploy_stage"], "deploy_qspi")
        self.assertTrue((self.root / "out/qspi.receipt").exists())

    def test_release_does_not_deploy_without_explicit_deploy_flag(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt"),
            "deploy_zybo": {
                "kind": "deploy", "needs": ["hardware"], "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target", "out/deployed.txt", "{target}"],
                "outputs": ["out/deployed.txt"], "toolchains": ["python"],
            },
        }
        engine = self._engine(stages)
        result = engine.release(target="zybo")
        self.assertFalse(result["deploy_requested"])
        self.assertFalse((self.root / "out/deployed.txt").exists())
        explicit = engine.release(target="zybo", deploy=True)
        self.assertTrue(explicit["deploy_requested"])
        self.assertTrue((self.root / "out/deployed.txt").is_file())

    def test_output_glob_cache_rehashes_all_concrete_artifacts(self) -> None:
        stage = _count_stage("out/build-id-001/design.bit", "out/build-id-001/build-count.txt")
        stage["outputs"] = ["out/build-id-*/design.bit", "out/build-id-*/build-count.txt"]
        engine = self._engine({"hardware": stage})
        engine.build("hardware")
        second = engine.build("hardware")
        self.assertTrue(second["stages"][0]["cache_hit"])
        extra = self.root / "out/build-id-002/design.bit"
        extra.parent.mkdir(parents=True)
        extra.write_text("untracked matching artifact", encoding="utf-8")
        third = engine.build("hardware")
        self.assertFalse(third["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/build-id-001/build-count.txt").read_text(encoding="utf-8"), "2")

    def test_target_profile_change_invalidates_target_bound_stage(self) -> None:
        stage = _count_stage("out/hardware.bit", "out/hardware-count.txt")
        stage["target"] = "zybo"
        profile = _write_profile(self.root, {"hardware": stage}, targets={"zybo": {"board_id": "board-a"}})
        engine = FlowEngine(load_profile(profile))
        engine.build("hardware")
        profile = _write_profile(self.root, {"hardware": stage}, targets={"zybo": {"board_id": "board-b"}})
        engine = FlowEngine(load_profile(profile))
        result = engine.build("hardware")
        self.assertFalse(result["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "2")

    def test_uart_profile_changes_do_not_invalidate_pl_but_hardware_identity_does(self) -> None:
        hardware = _count_stage("out/hardware.bit", "out/hardware-count.txt")
        hardware["target"] = "zybo"
        board_test = _count_stage("out/board/report.json", "out/board/count.txt", needs=["hardware"])
        board_test["kind"] = "validate"
        board_test["target"] = "zybo"

        def targets(*, part: str, board_id: str, prompt: str) -> dict[str, dict[str, Any]]:
            return {
                "zybo": {
                    "family": "amd-zynq-7000",
                    "part": part,
                    "board_part": "digilentinc.com:zybo-z7-20:part0:1.2",
                    "board_id": board_id,
                    "uart": {"serial_id": "usb-Digilent-if01", "prompt_regex": prompt},
                }
            }

        profile = _write_profile(self.root, {"hardware": hardware, "board_smoke": board_test},
                                 suites={"board": ["board_smoke"]},
                                 targets=targets(part="xc7z020clg400-1", board_id="zybo-a", prompt=">"))
        engine = FlowEngine(load_profile(profile))
        engine.build("hardware", target="zybo")
        first_validation = engine.validate("board", target="zybo")["stages"][0]

        profile = _write_profile(self.root, {"hardware": hardware, "board_smoke": board_test},
                                 suites={"board": ["board_smoke"]},
                                 targets=targets(part="xc7z020clg400-1", board_id="zybo-a", prompt="READY>"))
        engine = FlowEngine(load_profile(profile))
        uart_only = engine.build("hardware", target="zybo")
        self.assertTrue(uart_only["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "1")
        second_validation = engine.validate("board", target="zybo")["stages"][0]
        self.assertNotEqual(first_validation["fingerprint"], second_validation["fingerprint"])

        profile = _write_profile(self.root, {"hardware": hardware, "board_smoke": board_test},
                                 suites={"board": ["board_smoke"]},
                                 targets=targets(part="xc7z010clg400-1", board_id="zybo-a", prompt="READY>"))
        engine = FlowEngine(load_profile(profile))
        part_changed = engine.build("hardware", target="zybo")
        self.assertFalse(part_changed["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "2")

        profile = _write_profile(self.root, {"hardware": hardware, "board_smoke": board_test},
                                 suites={"board": ["board_smoke"]},
                                 targets=targets(part="xc7z010clg400-1", board_id="zybo-b", prompt="READY>"))
        engine = FlowEngine(load_profile(profile))
        board_id_changed = engine.build("hardware", target="zybo")
        self.assertFalse(board_id_changed["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "3")

    def test_uart_transport_changes_preserve_pl_and_jtag_fingerprints(self) -> None:
        hardware = _count_stage("out/hardware.bit", "out/hardware-count.txt")
        hardware["target"] = "zybo"
        deploy_jtag = {
            "kind": "deploy",
            "needs": ["hardware"],
            "runner": "amd.zynq.jtag",
            "target": "zybo",
            "options": {"receipt": "out/jtag.receipt"},
            "outputs": ["out/jtag.receipt"],
        }
        board_smoke = {
            "kind": "validate",
            "needs": ["deploy_jtag"],
            "runner": "amd.uart.commands",
            "target": "zybo",
            "options": {"receipt": "out/uart-validation.receipt"},
            "outputs": ["out/uart-validation.receipt"],
        }
        providers = {
            "amd.zynq.jtag": FakeTransportProvider(),
            "amd.uart.commands": FakeTransportProvider(),
        }

        def targets(*, port: str, baudrate: int, prompt: str) -> dict[str, dict[str, Any]]:
            return {
                "zybo": {
                    "family": "amd-zynq-7000",
                    "part": "xc7z020clg400-1",
                    "board_id": "zybo-a",
                    "jtag": {"pl_target_pattern": "xc7z020"},
                    "uart": {"port": port, "baudrate": baudrate, "prompt_regex": prompt},
                }
            }

        stages = {"hardware": hardware, "deploy_jtag": deploy_jtag, "board_smoke": board_smoke}
        profile = _write_profile(
            self.root, stages, suites={"board": ["board_smoke"]},
            targets=targets(port="/dev/ttyUSB0", baudrate=115200, prompt=">"),
        )
        engine = FlowEngine(load_profile(profile), providers=providers)
        first_build = engine.build("hardware", target="zybo")
        self.assertFalse(first_build["stages"][0]["cache_hit"])
        engine.deploy("zybo")
        first_deploy_record = engine.store.load()["stages"]["deploy_jtag"]
        first_deploy_fingerprint = first_deploy_record["fingerprint"]
        valid, reason, cached_deploy = engine._cache_state("deploy_jtag", target="zybo")
        self.assertTrue(valid, reason)
        self.assertEqual(cached_deploy["fingerprint"], first_deploy_fingerprint)
        first_validation = engine.validate("board", target="zybo")["stages"][0]

        profile = _write_profile(
            self.root, stages, suites={"board": ["board_smoke"]},
            targets=targets(port="/dev/ttyUSB1", baudrate=921600, prompt="READY>"),
        )
        engine = FlowEngine(load_profile(profile), providers=providers)
        uart_only_build = engine.build("hardware", target="zybo")
        self.assertTrue(uart_only_build["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "1")
        valid, reason, cached_deploy = engine._cache_state("deploy_jtag", target="zybo")
        self.assertTrue(valid, reason)
        self.assertEqual(cached_deploy["fingerprint"], first_deploy_fingerprint)

        second_validation = engine.validate("board", target="zybo")["stages"][0]
        self.assertNotEqual(first_validation["fingerprint"], second_validation["fingerprint"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "1")

    def test_cli_plan_config_option_works_after_subcommand_and_outputs_json(self) -> None:
        profile = _write_profile(self.root, {"hardware": _count_stage("out/hardware.bit")})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["plan", "--config", str(profile), "--json"])
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["commands_launched"])
        self.assertFalse((self.root / "out/hardware.bit").exists())

    def test_uart_only_profile_change_preserves_jtag_fingerprint_but_updates_uart_validation(self) -> None:
        hardware = _count_stage("out/hardware.bit", "out/hardware-count.txt")
        jtag = {"kind": "deploy", "runner": "amd.zynq.jtag", "target": "zybo",
                "needs": ["hardware"], "toolchains": [], "options": {"receipt": "out/jtag.receipt"},
                "outputs": ["out/jtag.receipt"]}
        uart = {"kind": "validate", "runner": "amd.uart.commands", "target": "zybo",
                "needs": ["hardware"], "toolchains": [], "options": {"receipt": "out/uart.receipt"},
                "outputs": ["out/uart.receipt"]}
        stages = {"hardware": hardware, "deploy_jtag": jtag, "board_smoke": uart}
        suites = {"board": ["board_smoke"]}
        provider = FakeTransportProvider()
        first_targets = {
            "zybo": {
                "family": "amd-zynq-7000", "part": "xc7z020clg400-1",
                "board_part": "digilentinc.com:zybo-z7-20:part0:1.2", "board_id": "zybo-serial-a",
                "jtag": {"hw_server_url": "tcp:127.0.0.1:3121", "pl_target_pattern": "xc7z020*"},
                "uart": {"serial_id": "usb-Digilent-if01", "prompt_regex": r"[>#]\s*$"},
            }
        }
        profile = _write_profile(self.root, stages, suites=suites, targets=first_targets)
        engine = FlowEngine(load_profile(profile), providers={"amd.zynq.jtag": provider,
                                                             "amd.uart.commands": provider})
        engine.build("hardware", target="zybo")
        first_jtag = engine.deploy("zybo", stage_name="deploy_jtag")["stages"][0]
        first_uart = engine.validate("board", target="zybo")["stages"][0]

        second_targets = {
            "zybo": {
                "family": "amd-zynq-7000", "part": "xc7z020clg400-1",
                "board_part": "digilentinc.com:zybo-z7-20:part0:1.2", "board_id": "zybo-serial-a",
                "jtag": {"hw_server_url": "tcp:127.0.0.1:3121", "pl_target_pattern": "xc7z020*"},
                "uart": {"serial_id": "usb-Digilent-if01", "prompt_regex": r"READY>\s*$"},
            }
        }
        profile = _write_profile(self.root, stages, suites=suites, targets=second_targets)
        engine = FlowEngine(load_profile(profile), providers={"amd.zynq.jtag": provider,
                                                             "amd.uart.commands": provider})
        hardware_result = engine.build("hardware", target="zybo")
        self.assertTrue(hardware_result["stages"][0]["cache_hit"])
        second_jtag = engine.deploy("zybo", stage_name="deploy_jtag")["stages"][0]
        second_uart = engine.validate("board", target="zybo")["stages"][0]
        self.assertEqual(first_jtag["fingerprint"], second_jtag["fingerprint"])
        self.assertNotEqual(first_uart["fingerprint"], second_uart["fingerprint"])
        self.assertEqual((self.root / "out/hardware-count.txt").read_text(encoding="utf-8"), "1")

    def test_cli_help_uses_chinese_for_default_help_action(self) -> None:
        for arguments in (["--help"], ["build", "--help"]):
            stdout = io.StringIO()
            with self.subTest(arguments=arguments):
                with redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
                    main(arguments)
                self.assertEqual(caught.exception.code, 0)
                self.assertIn("显示此帮助信息并退出", stdout.getvalue())
                self.assertNotIn("show this help message", stdout.getvalue())

    def test_human_plan_translates_state_labels_but_keeps_stage_ids(self) -> None:
        from fpga_flow.cli import _human

        stdout = io.StringIO()
        result = {"project": "sample", "target": "board", "stages": [
            {"stage": "hardware", "action": "run", "status": "pending", "detail": "暂无记录"}
        ]}
        with redirect_stdout(stdout):
            _human(result, "plan")
        output = stdout.getvalue()
        self.assertIn("动作=执行", output)
        self.assertIn("状态=待执行", output)
        self.assertIn("阶段 hardware", output)
        self.assertNotIn("pending", output)

    def test_cli_requires_explicit_deploy_stage_selector_options(self) -> None:
        from fpga_flow.cli import build_parser

        deploy = build_parser().parse_args(["deploy", "--target", "zybo", "--stage", "deploy_jtag"])
        self.assertEqual((deploy.target, deploy.stage), ("zybo", "deploy_jtag"))
        release = build_parser().parse_args([
            "release", "--target", "zybo", "--deploy", "--deploy-stage", "deploy_qspi"
        ])
        self.assertTrue(release.deploy)
        self.assertEqual(release.deploy_stage, "deploy_qspi")

    def test_same_engine_runs_distinct_rtl_ps_and_hls_ps_profiles(self) -> None:
        rtl_root = self.root / "rtl-ps-project"
        hls_root = self.root / "hls-ps-project"
        rtl_root.mkdir()
        hls_root.mkdir()
        rtl_profile = _write_profile(rtl_root, {
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt"),
            "app": _count_stage("out/app.elf", "out/app-count.txt", needs=["hardware"]),
        })
        hls_profile = _write_profile(hls_root, {
            "hls": _count_stage("out/hls/component.ip", "out/hls-count.txt"),
            "hardware": _count_stage("out/hardware.bit", "out/hardware-count.txt", needs=["hls"]),
            "app": _count_stage("out/app.elf", "out/app-count.txt", needs=["hardware"]),
        })
        rtl_engine = FlowEngine(load_profile(rtl_profile))
        hls_engine = FlowEngine(load_profile(hls_profile))

        rtl_first = rtl_engine.build("app", with_deps=True)
        hls_first = hls_engine.build("app", with_deps=True)
        self.assertEqual([row["stage"] for row in rtl_first["stages"]], ["hardware", "app"])
        self.assertEqual([row["stage"] for row in hls_first["stages"]], ["hls", "hardware", "app"])

        rtl_second = rtl_engine.build("app", with_deps=True)
        hls_second = hls_engine.build("app", with_deps=True)
        self.assertTrue(all(row["cache_hit"] for row in rtl_second["stages"]))
        self.assertTrue(all(row["cache_hit"] for row in hls_second["stages"]))

        rtl_manifest = rtl_engine.store.load()
        hls_manifest = hls_engine.store.load()
        self.assertNotEqual(rtl_engine.store.manifest_path, hls_engine.store.manifest_path)
        self.assertEqual(rtl_manifest["project"]["name"], "fixture-amd-project")
        self.assertEqual(hls_manifest["project"]["name"], "fixture-amd-project")
        self.assertIn("hardware", rtl_manifest["stages"])
        self.assertNotIn("hls", rtl_manifest["stages"])
        self.assertIn("hls", hls_manifest["stages"])
        self.assertTrue((hls_root / "out/hls/component.ip").is_file())
        self.assertTrue((rtl_root / "out/app.elf").is_file())
        self.assertTrue((hls_root / "out/app.elf").is_file())

    def test_unsupported_provider_fails_closed(self) -> None:
        profile = _write_profile(self.root, {
            "deploy_zybo": {"kind": "deploy", "runner": "amd.zynq.jtag", "target": "zybo",
                            "outputs": ["out/deployed.txt"], "toolchains": []},
        })
        engine = FlowEngine(load_profile(profile), providers={})
        with self.assertRaises(FlowError) as raised:
            engine.deploy("zybo")
        self.assertEqual(raised.exception.code, "FLOW-PROVIDER-UNSUPPORTED")
        self.assertFalse((self.root / "out/deployed.txt").exists())

    def test_program_flash_uses_vitis_version_probe_and_execution_command_is_fingerprinted(self) -> None:
        profile = _write_profile(self.root, {
            "program_flash": {
                "kind": "build",
                "runner": "amd.qspi.program",
                "target": "zybo",
                "inputs": ["tools/*.py"],
                "outputs": ["out/flash.receipt", "out/flash.count"],
                "toolchains": ["program_flash"],
            },
        }, expected_python=None)
        tools = self.root / "tools"
        (tools / "vitis_version.py").write_text(
            "print('Vitis v2025.2.1')\n", encoding="utf-8"
        )
        for version in ("v1", "v2"):
            (tools / f"flash_program_{version}.py").write_text(
                "from pathlib import Path\n"
                "out = Path('out')\n"
                "out.mkdir(exist_ok=True)\n"
                "count = out / 'flash.count'\n"
                "n = int(count.read_text()) if count.exists() else 0\n"
                "count.write_text(str(n + 1))\n"
                f"(out / 'flash.receipt').write_text('programmed {version}')\n",
                encoding="utf-8",
            )

        def configure_programmer(version: str) -> None:
            command_line = "command = [" + ", ".join(_quote(x) for x in (sys.executable, f"tools/flash_program_{version}.py")) + "]"
            content = profile.read_text(encoding="utf-8")
            section = "[toolchains.program_flash]"
            if section not in content:
                content += ("\n" + section + "\n" + command_line + "\n"
                            + "version_command = [" + ", ".join(_quote(x) for x in (sys.executable, "tools/vitis_version.py")) + "]\n"
                            + "version_args = []\nexpected_version = \"Vitis v2025.2.1\"\n")
            else:
                lines = content.splitlines()
                section_seen = False
                for index, line in enumerate(lines):
                    if line.strip() == section:
                        section_seen = True
                        continue
                    if section_seen and line.startswith("["):
                        break
                    if section_seen and line.startswith("command = "):
                        lines[index] = command_line
                        break
                content = "\n".join(lines) + "\n"
            profile.write_text(content, encoding="utf-8")

        configure_programmer("v1")
        engine = FlowEngine(load_profile(profile), providers={"amd.qspi.program": FakeFlashProvider()})
        doctor = engine.doctor(target="zybo")
        self.assertEqual(doctor["status"], "succeeded")
        self.assertIn("Vitis v2025.2.1", doctor["toolchain_details"]["program_flash"]["reported_version"])
        first = engine.build("program_flash", target="zybo")
        self.assertFalse(first["stages"][0]["cache_hit"])
        second = engine.build("program_flash", target="zybo")
        self.assertTrue(second["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/flash.count").read_text(encoding="utf-8"), "1")

        configure_programmer("v2")
        engine = FlowEngine(load_profile(profile), providers={"amd.qspi.program": FakeFlashProvider()})
        changed = engine.build("program_flash", target="zybo")
        self.assertFalse(changed["stages"][0]["cache_hit"])
        self.assertEqual((self.root / "out/flash.count").read_text(encoding="utf-8"), "2")
        self.assertEqual((self.root / "out/flash.receipt").read_text(encoding="utf-8"), "programmed v2")

    def test_status_reports_last_explicit_deploy_result_without_changing_plan_guard(self) -> None:
        stages = {
            "hardware": _count_stage("out/hardware.bit"),
            "deploy_jtag": {
                "kind": "deploy",
                "needs": ["hardware"],
                "target": "zybo",
                "command": [sys.executable, "tools/fake_stage.py", "target",
                            "out/jtag.receipt", "{target}"],
                "outputs": ["out/jtag.receipt"],
                "toolchains": ["python"],
            },
        }
        engine = self._engine(
            stages, targets={"zybo": {"family": "amd-zynq-7000"}}
        )
        engine.build("hardware")
        engine.deploy("zybo", stage_name="deploy_jtag")

        planned = next(row for row in engine.plan(target="zybo")["stages"]
                       if row["stage"] == "deploy_jtag")
        current = next(row for row in engine.status(target="zybo")["stages"]
                       if row["stage"] == "deploy_jtag")
        self.assertEqual(planned["action"], "explicit-deploy-only")
        self.assertEqual(planned["status"], "blocked")
        self.assertEqual(current["action"], "explicit-deploy-only")
        self.assertEqual(current["status"], "succeeded")
        self.assertEqual(current["last_status"], "succeeded")

    def test_status_detects_tampered_run_evidence(self) -> None:
        engine = self._engine({"hardware": _count_stage("out/hardware.bit")})
        run = engine.build("hardware")["stages"][0]
        run_dir = engine.store.run_path(run["run_id"]).parent
        (run_dir / "stdout.log").write_text("tampered", encoding="utf-8")
        status = engine.status()
        self.assertEqual(status["stages"][0]["last_evidence"], "tampered-or-missing")


if __name__ == "__main__":
    unittest.main()
