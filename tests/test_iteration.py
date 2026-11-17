from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fpga_flow.cli import _compact_json_result
from fpga_flow.config import load_profile
from fpga_flow.engine import FlowEngine, FlowError
from fpga_flow.locking import ProjectRunLock


FAKE_STAGE = """from pathlib import Path
import sys
import time

mode = sys.argv[1]
output = Path(sys.argv[2])
output.parent.mkdir(parents=True, exist_ok=True)
if mode == "fail":
    raise SystemExit(23)
if mode == "write":
    output.write_text(sys.argv[3], encoding="utf-8")
    raise SystemExit(0)
if mode == "sleep":
    time.sleep(float(sys.argv[3]))
    output.write_text("finished", encoding="utf-8")
    raise SystemExit(0)
if mode == "barrier":
    group = Path(sys.argv[3])
    peer = sys.argv[4]
    group.mkdir(parents=True, exist_ok=True)
    (group / output.parent.name).write_text("started", encoding="utf-8")
    deadline = time.monotonic() + 4
    while not (group / peer).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not (group / peer).exists():
        raise SystemExit(24)
    output.write_text("both-started", encoding="utf-8")
    raise SystemExit(0)
output.write_text("finished", encoding="utf-8")
"""


def _quote(value):
    return json.dumps(value, ensure_ascii=False)


def _toml(value):
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    raise TypeError(value)


def _write_profile(root, stages, *, suites=None, max_parallel=1,
                   hardware_identity=None, extra_toolchains=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "fake_stage.py").write_text(FAKE_STAGE, encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "seed.txt").write_text("seed", encoding="utf-8")
    lines = [
        "schema_version = 1",
        "",
        "[project]",
        'name = "synthetic-amd-fixture"',
        "",
        "[flow]",
        'output_dir = "out/fpga-flow"',
        'default_target = "board_a"',
        f"max_parallel_stages = {max_parallel}",
        "",
        "[toolchains.python]",
        f"command = [{_quote(sys.executable)}]",
        'version_args = ["--version"]',
        'expected_version = "Python "',
        "",
        "[targets.board_a]",
        'family = "amd-zynq-7000"',
    ]
    if hardware_identity:
        lines.append("hardware_identity = " + _toml(hardware_identity))
    for name, spec in (extra_toolchains or {}).items():
        lines += ["", f"[toolchains.{name}]", f"command = {_toml(spec['command'])}",
                  f"version_args = {_toml(spec.get('version_args', ['--version']))}"]
        if spec.get("expected_version") is not None:
            lines.append(f"expected_version = {_quote(spec['expected_version'])}")
    for name, stage in stages.items():
        lines += ["", f"[stages.{name}]", f"kind = {_quote(stage['kind'])}"]
        lines.append("needs = " + _toml(stage.get("needs", [])))
        lines.append("inputs = " + _toml(stage.get("inputs", ["src/seed.txt", "tools/fake_stage.py"])))
        lines.append("outputs = " + _toml(stage.get("outputs", [])))
        if "command" in stage:
            lines.append("command = " + _toml(stage["command"]))
        if stage.get("target") is not None:
            lines.append(f"target = {_quote(stage['target'])}")
        if "toolchains" in stage:
            lines.append("toolchains = " + _toml(stage["toolchains"]))
        elif stage.get("kind") in {"build", "test"}:
            lines.append('toolchains = ["python"]')
        if "resources" in stage:
            lines.append("resources = " + _toml(stage["resources"]))
        if "side_effects" in stage:
            lines.append("side_effects = " + _toml(stage["side_effects"]))
        if stage.get("destructive"):
            lines.append("destructive = true")
        if "timeout_seconds" in stage:
            lines.append(f"timeout_seconds = {stage['timeout_seconds']}")
        if "success_markers" in stage:
            lines.append("success_markers = " + _toml(stage["success_markers"]))
    if suites:
        lines += ["", "[suites]"]
        for name, selected in suites.items():
            lines.append(f"{name} = " + _toml(selected))
    path = root / "fpga-flow.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_stage(output, *, kind="build", needs=(), mode="write", value="ok",
                 target=None, **extra):
    command = [sys.executable, "tools/fake_stage.py", mode, output]
    if mode == "sleep":
        command.append(value)
    elif mode == "write":
        command.append(value)
    return {
        "kind": kind, "needs": list(needs), "command": command,
        "outputs": [output], "target": target, **extra,
    }


class FlowIterationTests(unittest.TestCase):
    def test_default_json_compacts_large_hash_maps_but_keeps_run_reference(self):
        result = {"status": "succeeded", "stages": [{
            "stage": "build", "run_id": "run-123",
            "inputs": {"a.v": "a" * 64, "b.v": "b" * 64},
            "outputs": {"design.bit": "c" * 64},
            "evidence": {"stdout.log": "d" * 64},
        }]}
        compact = _compact_json_result(result)
        stage = compact["stages"][0]
        self.assertNotIn("inputs", stage)
        self.assertNotIn("outputs", stage)
        self.assertNotIn("evidence", stage)
        self.assertEqual(stage["input_count"], 2)
        self.assertEqual(stage["output_count"], 1)
        self.assertEqual(stage["evidence_count"], 1)
        self.assertEqual(stage["record_path"], "runs/run-123/run.json")

    def test_resume_advises_side_effect_ack_without_running_the_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _write_stage("out/saves/check.txt", kind="validate",
                                 side_effects=["persistent-storage"])
            path = _write_profile(root, {"save_check": stage}, suites={"storage": ["save_check"]})
            engine = FlowEngine.from_file(path)
            result = engine.resume(suite="storage")
            self.assertIn("--allow-side-effects", result["next_action"]["command"])
            self.assertFalse((root / "out/saves/check.txt").exists())

    def test_resume_suggests_watch_for_a_failed_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = _write_stage("out/design.bit", mode="fail")
            path = _write_profile(root, {"hardware": stage})
            engine = FlowEngine.from_file(path)
            with self.assertRaises(FlowError):
                engine.build("hardware")
            result = engine.resume()
            self.assertEqual(result["next_action"]["kind"], "inspect-run")
            self.assertIn("watch", result["next_action"]["command"])
            self.assertNotIn("--deploy", result["next_action"]["command"])

    def test_profile_limit_and_resource_locks_bound_parallel_dag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _write_stage("out/first/result.txt", mode="barrier",
                                 value="out/barrier", resources=["independent-output:first"])
            first["command"] = [sys.executable, "tools/fake_stage.py", "barrier",
                                "out/first/result.txt", "out/barrier", "second"]
            second = _write_stage("out/second/result.txt", mode="barrier",
                                  value="out/barrier", resources=["independent-output:second"])
            second["command"] = [sys.executable, "tools/fake_stage.py", "barrier",
                                 "out/second/result.txt", "out/barrier", "first"]
            path = _write_profile(root, {"first": first, "second": second}, max_parallel=2)
            result = FlowEngine.from_file(path).release()
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual((root / "out/first/result.txt").read_text(), "both-started")
            self.assertEqual((root / "out/second/result.txt").read_text(), "both-started")

    def test_release_waits_for_checks_before_deploy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hardware = _write_stage("out/hardware/design.bit")
            check = _write_stage("out/check/result.txt", kind="test", mode="fail")
            deploy = _write_stage("out/deploy/receipt.txt", kind="deploy",
                                  needs=("hardware",), target="board_a", mode="write",
                                  destructive=True)
            path = _write_profile(root, {
                "hardware": hardware, "host_check": check, "deploy_jtag": deploy,
            }, max_parallel=2)
            engine = FlowEngine.from_file(path)
            with self.assertRaises(FlowError):
                engine.release(target="board_a", deploy=True, deploy_stage="deploy_jtag",
                               allow_destructive=True)
            self.assertFalse((root / "out/deploy/receipt.txt").exists())

    def test_doctor_only_probes_tools_for_selected_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hardware = _write_stage("out/design.bit")
            app = _write_stage("out/app.elf", kind="build", needs=("hardware",),
                               toolchains=["missing_tool"])
            path = _write_profile(root, {"hardware": hardware, "app": app},
                                  extra_toolchains={"missing_tool": {
                                      "command": "no-such-fpga-tool", "version_args": ["--version"]}})
            engine = FlowEngine.from_file(path)
            scoped = engine.doctor(stage="hardware")
            self.assertEqual(scoped["status"], "succeeded")
            self.assertEqual(list(scoped["toolchain_details"]), ["python"])
            full = engine.doctor()
            self.assertEqual(full["status"], "failed")
            self.assertEqual(full["toolchain_details"]["missing_tool"]["status"], "missing")

    def test_same_hardware_identity_is_locked_across_project_output_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_root = root / "global-locks"
            identity = {"serial_number": "device-123"}
            stage = _write_stage("out/board/result.txt", kind="validate", mode="sleep",
                                 value="1.0", target="board_a")
            path_one = _write_profile(root / "one", {"board_check": stage},
                                      suites={"board": ["board_check"]}, hardware_identity=identity)
            path_two = _write_profile(root / "two", {"board_check": stage},
                                      suites={"board": ["board_check"]}, hardware_identity=identity)
            one, two = FlowEngine.from_file(path_one), FlowEngine.from_file(path_two)
            finished = threading.Event()
            outcome = []

            def first_run():
                try:
                    outcome.append(one.validate("board", target="board_a"))
                except Exception as exc:
                    outcome.append(exc)
                finally:
                    finished.set()

            with patch.dict(os.environ, {"FPGA_FLOW_LOCK_DIR": str(lock_root)}):
                thread = threading.Thread(target=first_run)
                thread.start()
                deadline = time.monotonic() + 5
                owner = None
                while time.monotonic() < deadline:
                    lock_file = next(iter(lock_root.glob("*.lock")), None)
                    if lock_file is not None:
                        owner = ProjectRunLock.read_owner(lock_file)
                        if owner and owner.get("active_stages"):
                            break
                    time.sleep(0.02)
                self.assertTrue(owner and owner.get("active_stages"))
                with self.assertRaises(FlowError) as raised:
                    two.validate("board", target="board_a")
                self.assertEqual(raised.exception.code, "FLOW-TARGET-LOCK-BUSY")
                thread.join(timeout=5)
                self.assertTrue(finished.is_set())
                self.assertFalse(isinstance(outcome[0], Exception))


if __name__ == "__main__":
    unittest.main()
