from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from fpga_flow.cli import main
from fpga_flow.locking import ProjectRunLock, RunLockError
from fpga_flow.workspace_dag import WorkspaceDAG, WorkspaceError, load_workspace, tree_hash


class WorkspaceDAGTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.canonical = self.root / "canonical"
        self.consumer = self.root / "consumer"
        self._make_project(self.canonical, "canonical")
        self._make_project(self.consumer, "consumer")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_project(self, root: Path, project_id: str) -> None:
        (root / "ip").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "VERSION").write_text(("0.3.0-dev.3" if project_id == "canonical" else "1.0.0") + "\n", encoding="utf-8")
        (root / "tests/tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
        (root / "fpga-project.toml").write_text(
            f'''schema_version = 1
[project]
id = "{project_id}"
name = "{project_id}"
profile = "vivado-ip-package"
[ip_package]
component_xml = "ip/component.xml"
version_file = "VERSION"
source_roots = ["ip"]
[tests]
source_roots = ["tests"]
[[tests.bench]]
name = "tb"
sources = ["tests/tb.sv"]
top = "tb"
[build]
output_dir = "build/fpga-flow"
''', encoding="utf-8")

    def _archive_pair(self, *, bad_sums: bool = False, traversal: bool = False) -> tuple[dict, dict]:
        release_root = self.canonical / "build/fpga-flow/runs/fake-release/work/artifacts/releases"
        release_root.mkdir(parents=True, exist_ok=True)
        version = "0.3.0-dev.3"
        vlnv = "panfpga.org:video:embedded_video_ip:1.0"
        ip_name = f"EmbeddedVideoIP-{version}-vivado-2025.2.1.zip"
        ps_name = f"EmbeddedVideoIP-PS-Zynq7000-{version}.zip"
        ip_path, ps_path = release_root / ip_name, release_root / ps_name
        ip_metadata = {"version": version, "vlnv": vlnv, "core_revision": 5, "source_commit": "abc123",
                       "canonical_source_sha256": "a" * 64, "vivado_version": "2025.2.1"}
        ps_metadata = {"version": version, "source_commit": "abc123"}
        with zipfile.ZipFile(ip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("embedded-video-ip/VERSION", version + "\n")
            archive.writestr("embedded-video-ip/RELEASE-METADATA.json", json.dumps(ip_metadata))
            archive.writestr("embedded-video-ip/ip_repo/panfpga/embedded_video_ip/component.xml",
                '<spirit:component xmlns:spirit="urn:spirit" xmlns:xilinx="urn:xilinx"><xilinx:coreRevision>5</xilinx:coreRevision></spirit:component>')
            archive.writestr("embedded-video-ip/ip_repo/panfpga/embedded_video_ip/src/core.sv", "module core; endmodule\n")
            if traversal:
                archive.writestr("embedded-video-ip/../../escape.txt", "bad")
        with zipfile.ZipFile(ps_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("embedded-video-ip-ps-zynq7000/RELEASE-METADATA.json", json.dumps(ps_metadata))
            archive.writestr("embedded-video-ip-ps-zynq7000/Video/Lib/video_out.h", "#define VERSION 3\n")
        ip_hash = hashlib.sha256(ip_path.read_bytes()).hexdigest()
        ps_hash = hashlib.sha256(ps_path.read_bytes()).hexdigest()
        sums_path = release_root / "SHA256SUMS"
        sum_ip = ("0" if bad_sums else ip_hash)
        sums_path.write_text(f"{sum_ip}  {ip_name}\n{ps_hash}  {ps_name}\n", encoding="ascii")
        sums_hash = hashlib.sha256(sums_path.read_bytes()).hexdigest()
        outputs = {
            (ip_path.relative_to(self.canonical)).as_posix(): ip_hash,
            (ps_path.relative_to(self.canonical)).as_posix(): ps_hash,
            (sums_path.relative_to(self.canonical)).as_posix(): sums_hash,
        }
        return {"outputs": outputs, "ip": ip_path, "ps": ps_path, "sums": sums_path}, {
            "stage": "release", "status": "succeeded", "run_id": "fake-release",
            "output_root": "build/fpga-flow/runs/fake-release/work", "outputs": outputs,
        }

    def _workspace(self, *, bad_sums: bool = False, traversal: bool = False, fail_first: bool = False) -> tuple[WorkspaceDAG, dict]:
        pair, release_record = self._archive_pair(bad_sums=bad_sums, traversal=traversal)
        manifest = self.consumer / "fpga-workspace.toml"
        manifest.write_text(f'''schema_version = 1
[workspace]
id = "unit-test-workspace"
output_dir = "build/workspace"
resource_lock_dir = "{(self.root / 'locks').as_posix()}"
[projects.canonical]
manifest = "../canonical/fpga-project.toml"
[projects.consumer]
manifest = "fpga-project.toml"
[[nodes]]
name = "release"
kind = "flow"
project = "canonical"
action = "release"
resources = ["vivado"]
[[nodes]]
name = "import"
kind = "artifact_import"
source_project = "canonical"
source_node = "release"
target_project = "consumer"
expected_version = "0.3.0-dev.3"
expected_vlnv = "panfpga.org:video:embedded_video_ip:1.0"
expected_core_revision = 5
ip_asset = "{pair['ip'].name}"
ps_asset = "{pair['ps'].name}"
ip_prefix = "embedded-video-ip"
ps_prefix = "embedded-video-ip-ps-zynq7000/Video/Lib"
ip_target = "ip-repo/embedded-video-ip"
ps_target = "src/Video/Lib"
dependency_name = "embedded_video_ip_hardware"
dependency_lock_file = "ip-repo/embedded-video-ip-DEPENDENCY-LOCK.json"
needs = ["release"]
resources = ["package-import"]
[[nodes]]
name = "after_import"
kind = "flow"
project = "consumer"
action = "release"
needs = ["import"]
''', encoding="utf-8")
        (self.consumer / "ip-repo/embedded-video-ip").mkdir(parents=True)
        (self.consumer / "ip-repo/embedded-video-ip/old.txt").write_text("old ip", encoding="utf-8")
        (self.consumer / "src/Video/Lib").mkdir(parents=True)
        (self.consumer / "src/Video/Lib/old.h").write_text("old ps", encoding="utf-8")
        (self.consumer / "ip-repo/embedded-video-ip-DEPENDENCY-LOCK.json").write_text('{"old":true}\n', encoding="utf-8")
        def fake_flow(project, node, run_dir, force, allow_side_effects, allow_destructive):
            if fail_first and node.name == "release":
                raise WorkspaceError("simulated release failure")
            if node.name == "release":
                return {"flow": {"status": "succeeded", "stages": [release_record]}}
            return {"flow": {"status": "succeeded", "stages": []}}
        return WorkspaceDAG.from_file(manifest, flow_runner=fake_flow), pair

    def test_plan_orders_cross_project_dependencies_without_running_commands(self):
        dag, _ = self._workspace()
        plan = dag.plan()
        self.assertEqual([item["node"] for item in plan["nodes"]], ["release", "import", "after_import"])
        self.assertFalse(plan["commands_launched"])

    def test_release_import_verifies_hashes_installs_both_archives_and_keeps_backups(self):
        dag, _ = self._workspace()
        result = dag.run()
        self.assertEqual(result["status"], "succeeded")
        ip_target = self.consumer / "ip-repo/embedded-video-ip"
        ps_target = self.consumer / "src/Video/Lib"
        self.assertTrue((ip_target / "ip_repo/panfpga/embedded_video_ip/src/core.sv").is_file())
        self.assertTrue((ps_target / "video_out.h").is_file())
        lock = json.loads((self.consumer / "ip-repo/embedded-video-ip-DEPENDENCY-LOCK.json").read_text())
        self.assertEqual(lock["version"], "0.3.0-dev.3")
        imported = result["nodes"]["import"]["installations"]
        self.assertEqual(len(imported), 3)
        for item in imported:
            self.assertTrue(item["backup_path"])
            self.assertEqual(item["previous_tree_sha256"], item["backup_tree_sha256"])
            self.assertTrue((dag.config.root / result["run_dir"] / item["backup_path"]).exists())

    def test_identical_reimport_keeps_dependency_lock_fingerprint_stable(self):
        dag, _ = self._workspace()
        first = dag.run()
        lock_path = self.consumer / "ip-repo/embedded-video-ip-DEPENDENCY-LOCK.json"
        lock_before = lock_path.read_bytes()

        second = dag.run()

        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(lock_path.read_bytes(), lock_before)
        install = second["nodes"]["import"]["installations"]
        self.assertEqual(install[0]["previous_tree_sha256"], install[0]["imported_tree_sha256"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_checksum_failure_preserves_vendor_trees_and_blocks_dependents(self):
        dag, _ = self._workspace(bad_sums=True)
        before_ip = tree_hash(self.consumer / "ip-repo/embedded-video-ip")
        before_ps = tree_hash(self.consumer / "src/Video/Lib")
        with self.assertRaises(WorkspaceError):
            dag.run()
        state = dag.status()["run"]
        self.assertEqual(state["nodes"]["import"]["status"], "failed")
        self.assertEqual(state["nodes"]["after_import"]["status"], "blocked")
        self.assertEqual(tree_hash(self.consumer / "ip-repo/embedded-video-ip"), before_ip)
        self.assertEqual(tree_hash(self.consumer / "src/Video/Lib"), before_ps)

    def test_archive_path_traversal_is_rejected_before_install(self):
        dag, _ = self._workspace(traversal=True)
        before = tree_hash(self.consumer / "ip-repo/embedded-video-ip")
        with self.assertRaises(WorkspaceError):
            dag.run()
        self.assertEqual(tree_hash(self.consumer / "ip-repo/embedded-video-ip"), before)
        self.assertFalse((self.consumer / "escape.txt").exists())

    def test_failed_producer_blocks_cross_project_import(self):
        dag, _ = self._workspace(fail_first=True)
        with self.assertRaises(WorkspaceError):
            dag.run()
        state = dag.status()["run"]
        self.assertEqual(state["nodes"]["release"]["status"], "failed")
        self.assertEqual(state["nodes"]["import"]["status"], "blocked")
        self.assertEqual(state["nodes"]["after_import"]["status"], "blocked")

    def test_resource_lock_serializes_shared_tool_names(self):
        dag, _ = self._workspace()
        node = dag.config.nodes["release"]
        first = dag._locks(node, "run-one", False)
        second = None
        try:
            second = dag._locks(node, "run-two", False)
            self.fail("second workspace acquired an already held Vivado resource")
        except RunLockError:
            pass
        finally:
            first.close()
            if second is not None:
                second.close()

    def test_cli_plan_accepts_workspace_option(self):
        dag, _ = self._workspace()
        output = tempfile.TemporaryFile(mode="w+")
        from contextlib import redirect_stdout
        with redirect_stdout(output):
            code = main(["--workspace", str(dag.config.path), "plan", "--json"])
        self.assertEqual(code, 0)
        output.seek(0)
        self.assertEqual(json.loads(output.read())["workspace"], "unit-test-workspace")
        output.close()

    def test_mid_import_failure_restores_all_previous_targets(self):
        dag, _ = self._workspace()
        ip_target = self.consumer / "ip-repo/embedded-video-ip"
        ps_target = self.consumer / "src/Video/Lib"
        before_ip, before_ps = tree_hash(ip_target), tree_hash(ps_target)
        original = __import__("fpga_flow.workspace_dag", fromlist=["os"]).os.replace
        failed = {"done": False}
        def fail_second_install(source, destination):
            if (not failed["done"] and ".fpga-flow-import-1-" in str(source)
                    and Path(destination) == ps_target):
                failed["done"] = True
                raise OSError("injected second target replace failure")
            return original(source, destination)
        from unittest.mock import patch
        with patch("fpga_flow.workspace_dag.os.replace", side_effect=fail_second_install):
            with self.assertRaises(WorkspaceError):
                dag.run()
        self.assertTrue(failed["done"])
        self.assertEqual(tree_hash(ip_target), before_ip)
        self.assertEqual(tree_hash(ps_target), before_ps)

    def test_optional_nodes_skip_by_default_and_run_on_request(self):
        dag, _ = self._workspace()
        path = dag.config.path
        text = path.read_text(encoding="utf-8")
        old = 'name = "after_import"\nkind = "flow"\n'
        new = old + 'optional = true\n'
        self.assertEqual(text.count(old), 1)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        dag = WorkspaceDAG.from_file(path, flow_runner=dag.flow_runner)
        fast = dag.run()
        self.assertEqual(fast["nodes"]["after_import"]["status"], "skipped")
        full = dag.run(include_optional=True)
        self.assertEqual(full["nodes"]["after_import"]["status"], "succeeded")

    def test_cycle_is_rejected(self):
        dag, _ = self._workspace()
        path = self.consumer / "fpga-workspace.toml"
        text = path.read_text(encoding="utf-8").replace('needs = ["release"]', 'needs = ["after_import"]', 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            load_workspace(path)


if __name__ == "__main__":
    unittest.main()
