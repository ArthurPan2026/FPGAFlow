from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import sys
from importlib.resources import files
import tempfile
import tomllib
import unittest

from fpga_flow.config import load_profile
from fpga_flow.engine import FlowEngine
from fpga_flow.models import BundleConfig
from fpga_flow.project import (
    ProjectError,
    inventory_data,
    inventory_toml,
    load_project,
    project_stage_inputs,
    xpr_sources,
)


class ProjectInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for directory in (
            "vivado/demo.srcs/sources_1/bd/system",
            "vivado/demo.srcs/utils_1/imports/synth_1",
            "src", "constraints", "vendor/evip/ip_repo/panfpga/embedded_video_ip/src",
            "software/src", "tests",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        files = {
            "src/top.sv": "module top; endmodule\n",
            "src/disabled.sv": "module unused; endmodule\n",
            "constraints/demo.xdc": "set_property PACKAGE_PIN A1 [get_ports clk]\n",
            "vivado/demo.srcs/sources_1/bd/system/system.bd": "{}\n",
            "vivado/demo.srcs/utils_1/imports/synth_1/old.dcp": "generated\n",
            "vendor/evip/VERSION": "1.2.0\n",
            "vendor/evip/ip_repo/panfpga/embedded_video_ip/component.xml": (
                "<component><fileSets><fileSet><file><name>src/ip.sv</name></file></fileSet></fileSets></component>\n"
            ),
            "vendor/evip/ip_repo/panfpga/embedded_video_ip/src/ip.sv": "module ip; endmodule\n",
            "software/app.yaml": "template: freertos\n",
            "software/src/main.c": "int main(void) { return 0; }\n",
            "tests/tb_top.sv": "module tb_top; endmodule\n",
            "vivado/demo.xpr": """<Project>
  <FileSets>
    <FileSet Name="sources_1" Type="DesignSrcs">
      <File Path="$PPRDIR/../src/top.sv"><FileInfo><Attr Name="UsedIn" Val="synthesis"/><Attr Name="UsedIn" Val="implementation"/></FileInfo></File>
      <File Path="$PPRDIR/../src/disabled.sv"><FileInfo><Attr Name="AutoDisabled" Val="1"/></FileInfo></File>
      <File Path="$PSRCDIR/sources_1/bd/system/system.bd"/>
    </FileSet>
    <FileSet Name="constrs_1" Type="Constrs">
      <File Path="$PPRDIR/../constraints/demo.xdc"><FileInfo><Attr Name="UsedIn" Val="implementation"/></FileInfo></File>
    </FileSet>
    <FileSet Name="utils_1" Type="Utils">
      <File Path="$PSRCDIR/utils_1/imports/synth_1/old.dcp"/>
    </FileSet>
  </FileSets>
  <Configuration><Option Name="IPRepoPath" Val="$PPRDIR/../vendor/evip/ip_repo"/></Configuration>
</Project>
""",
            "fpga-project.toml": """schema_version = 1

[project]
id = "demo-board"
name = "Demo Board"
profile = "zynq7000-vitis"

[board]
id = "demo-board"
part = "xc7z020clg400-1"

[vivado]
project = "vivado/demo.xpr"

[vitis]
app_config = "software/app.yaml"
source_roots = ["software/src"]

[dependencies.evip]
version = "1.2.0"
package_dir = "vendor/evip"
component_xml = "vendor/evip/ip_repo/panfpga/embedded_video_ip/component.xml"
metadata = ["vendor/evip/VERSION"]

[tests]
source_roots = ["tests"]
default_success_markers = ["TEST_PASS"]

[tests.suite_stages]
smoke = "rtl_smoke"

[[tests.bench]]
name = "tb_top"
suite = "smoke"
top = "tb_top"
sources = ["tests/tb_top.sv", "src/top.sv"]

[build]
output_dir = "build/fpga-flow"
""",
        }
        for relative, contents in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        self.project = load_project(self.root / "fpga-project.toml")
        self._add_test_adapters()

    def _add_test_adapters(self) -> None:
        path = self.root / "fpga-project.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        profile = tomllib.loads(files("fpga_flow").joinpath("profiles", data["project"]["profile"] + ".toml").read_text(encoding="utf-8"))
        additions = []
        for name, stage in profile["stages"].items():
            if stage.get("runner", "command") == "command":
                additions.extend([f"[adapters.{name}]",
                    "command = " + json.dumps([sys.executable, "--version"])])
        path.write_text(path.read_text(encoding="utf-8") + "\n" + "\n".join(additions) + "\n", encoding="utf-8")

    def test_board_profile_loads_with_external_state_and_argv_tools(self) -> None:
        descriptor = self.root / "fpga-project.toml"
        contents = descriptor.read_text(encoding="utf-8")
        descriptor.write_text(contents.replace('id = "demo-board"', 'id = "zybo_z7_20"'), encoding="utf-8")
        profile = load_profile(descriptor)
        self.assertIn("hardware", profile.stages)
        self.assertIsNotNone(profile.state_dir)
        self.assertFalse(profile.state_dir.is_relative_to(self.root))
        self.assertEqual(profile.toolchains["vivado"].command, ("vivado",))

    def test_ip_package_profile_loads_before_packaging(self) -> None:
        (self.root / "VERSION").write_text("1.2.0\n", encoding="utf-8")
        descriptor = self.root / "fpga-project.toml"
        descriptor.write_text('''schema_version = 1
[project]
id = "demo-ip"
name = "Demo IP"
profile = "vivado-ip-package"
[tools]
vivado_version = "2025.2.1"
[ip_package]
component_xml = "generated/component.xml"
version_file = "VERSION"
source_roots = ["src"]
[tests]
source_roots = ["tests"]
''', encoding="utf-8")
        self._add_test_adapters()
        profile = load_profile(descriptor)
        self.assertEqual(set(profile.stages), {"sim", "package", "release"})
        self.assertEqual(profile.toolchains["vivado"].command, ("vivado",))
        self.assertFalse((self.root / "generated/component.xml").exists())

    def test_xpr_resolves_exact_project_sources_and_filters_generated_utils(self) -> None:
        sources = xpr_sources(self.project)
        by_path = {source["path"]: source for source in sources}

        self.assertIn("src/top.sv", by_path)
        self.assertTrue(by_path["src/top.sv"]["active"])
        self.assertFalse(by_path["src/disabled.sv"]["active"])
        self.assertIn("constraints/demo.xdc", by_path)
        self.assertIn(
            "vendor/evip/ip_repo/panfpga/embedded_video_ip/src/ip.sv", by_path
        )
        self.assertNotIn("vivado/demo.srcs/utils_1/imports/synth_1/old.dcp", by_path)

    def test_run_outputs_cache_bundle_and_optional_export(self) -> None:
        descriptor = self.root / "fpga-project.toml"
        text = descriptor.read_text(encoding="utf-8").replace('id = "demo-board"', 'id = "zybo_z7_20"')
        descriptor.write_text(text, encoding="utf-8")
        profile = load_profile(descriptor)
        script = self.root / "produce.py"
        script.write_text('''import os
from pathlib import Path
out=Path(os.environ["FPGA_FLOW_OUTPUT_DIR"])
(out/"artifact.bin").write_bytes(b"deliverable")
Path("vendor.log").write_text("isolated")
''', encoding="utf-8")
        stage = replace(profile.stages["hardware"], inputs=("produce.py",),
            outputs=("build/fpga-flow/artifact.bin",), command=(sys.executable, "produce.py"),
            toolchains=(), success_markers=())
        project = replace(profile.project_descriptor, export_dir="deliveries", export_artifacts=("image",))
        profile = replace(profile, stages={"hardware": stage}, toolchains={},
            project_descriptor=project, state_dir=self.root.parent / (self.root.name + "-state"),
            bundle=BundleConfig(3, self.root / "build/fpga-flow/current", {"image": "build/fpga-flow/artifact.bin"}))
        self.addCleanup(__import__("shutil").rmtree, profile.state_dir, True)
        engine = FlowEngine(profile)
        first = engine.build("hardware")["stages"][0]
        output = next(iter(first["outputs"]))
        self.assertIn("/runs/", output)
        self.assertTrue((self.root / output).is_file())
        self.assertFalse((self.root / "vendor.log").exists())
        self.assertTrue((self.root / first["output_root"] / "vendor.log").is_file())
        second = engine.build("hardware")["stages"][0]
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["outputs"], second["outputs"])
        self.assertEqual(first["output_root"], second["output_root"])
        deliveries = list((self.root / "deliveries").iterdir())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual((deliveries[0] / "artifact.bin").read_bytes(), b"deliverable")
        (self.root / output).write_bytes(b"tampered")
        self.assertFalse(engine._cache_state("hardware")[0])

    def test_hardware_inputs_exclude_tests_disabled_sources_and_generated_checkpoints(self) -> None:
        inputs = set(project_stage_inputs(self.project, "hardware"))

        self.assertIn("vivado/demo.xpr", inputs)
        self.assertIn("src/top.sv", inputs)
        self.assertIn("constraints/demo.xdc", inputs)
        self.assertIn("vendor/evip/ip_repo/panfpga/embedded_video_ip/src/ip.sv", inputs)
        self.assertNotIn("src/disabled.sv", inputs)
        self.assertNotIn("tests/tb_top.sv", inputs)
        self.assertNotIn("vivado/demo.srcs/utils_1/imports/synth_1/old.dcp", inputs)

    def test_bd_references_and_run_hooks_are_part_of_hardware_closure(self) -> None:
        bd = self.root / "vivado/demo.srcs/sources_1/bd/system/system.bd"
        xci = bd.parent / "ip/core/core.xci"
        xci.parent.mkdir(parents=True)
        xci.write_text("{}", encoding="utf-8")
        bd.write_text(json.dumps({"design": {"xci_path": "ip/core/core.xci"}}), encoding="utf-8")
        hook = self.root / "src/hook.tcl"
        hook.write_text("set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]", encoding="utf-8")
        xpr = self.root / "vivado/demo.xpr"
        xpr.write_text(xpr.read_text(encoding="utf-8").replace("</Project>",
            '<Runs><Step PreStepTclHook="$PPRDIR/../src/hook.tcl"/></Runs></Project>'), encoding="utf-8")
        inputs = project_stage_inputs(self.project, "hardware")
        self.assertIn(xci.relative_to(self.root).as_posix(), inputs)
        self.assertIn("src/hook.tcl", inputs)
        xci.unlink()
        with self.assertRaises(ProjectError):
            project_stage_inputs(self.project, "hardware")

    def test_application_sources_do_not_change_hardware_or_platform_inputs(self) -> None:
        from fpga_flow.engine import _stage_contract, _stage_input_hashes
        from fpga_flow.fingerprint import stage_fingerprint
        descriptor = self.root / "fpga-project.toml"
        text = descriptor.read_text(encoding="utf-8").replace('id = "demo-board"', 'id = "zybo_z7_20"')
        descriptor.write_text(text, encoding="utf-8")
        profile = load_profile(descriptor)
        def fingerprint(name):
            inputs = project_stage_inputs(profile.project_descriptor, name)
            stage = replace(profile.stages[name], inputs=inputs)
            return stage_fingerprint(_stage_contract(profile, stage, profile.default_target),
                _stage_input_hashes(profile, stage), {}, {})
        before = {name: fingerprint(name) for name in ("hardware", "platform", "app")}
        (self.root / "software/src/main.c").write_text("int main(void) { return 1; }", encoding="utf-8")
        self.assertEqual(before["hardware"], fingerprint("hardware"))
        self.assertEqual(before["platform"], fingerprint("platform"))
        self.assertNotEqual(before["app"], fingerprint("app"))

    def test_inventory_toml_lists_sources_and_suite_status(self) -> None:
        data = inventory_data(
            self.project,
            {"rtl_smoke": {"status": "succeeded", "run_id": "run-1"}},
        )
        inventory = tomllib.loads(inventory_toml(data))

        paths = {item["path"] for item in inventory["sources"]}
        self.assertIn("vivado/demo.xpr", paths)
        self.assertIn("tests/tb_top.sv", paths)
        self.assertTrue(any(
            item["path"] == "tests/tb_top.sv" and item["role"] == "test_source"
            for item in inventory["sources"]
        ))
        self.assertIn("software/src/main.c", paths)
        self.assertEqual(inventory["tests"][0]["status"], "passed")
        self.assertEqual(inventory["tests"][0]["run_id"], "run-1")
        self.assertEqual(inventory["dependencies"][0]["version"], "1.2.0")

    def test_xpr_rejects_unresolved_variables_and_paths_outside_project(self) -> None:
        xpr = self.root / "vivado/demo.xpr"
        original = xpr.read_text(encoding="utf-8")
        xpr.write_text(original.replace("$PPRDIR/../src/top.sv", "$UNKNOWN/top.sv"), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "unsupported Vivado path variable"):
            xpr_sources(self.project)
        xpr.write_text(original.replace("$PPRDIR/../src/top.sv", "$PPRDIR/../../outside.sv"), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "escapes the project"):
            xpr_sources(self.project)


if __name__ == "__main__":
    unittest.main()
