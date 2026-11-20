from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from fpga_flow.bundle import refresh_bundle
from fpga_flow.config import load_profile
from fpga_flow.manifest import ManifestStore


class BundleTests(unittest.TestCase):
    def test_bundle_materializes_only_hash_verified_stage_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, payload in (
                ("out/fpga-flow/artifacts/bitstream.bit", b"bit"),
                ("out/fpga-flow/artifacts/hardware.xsa", b"xsa"),
                ("out/fpga-flow/artifacts/fsbl.elf", b"fsbl"),
                ("out/fpga-flow/artifacts/app.elf", b"app"),
                ("out/fpga-flow/artifacts/BOOT.BIN", b"boot"),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            config = root / "fpga-flow.toml"
            config.write_text(textwrap.dedent("""
                schema_version = 1
                [project]
                name = "bundle-test"
                [flow]
                output_dir = "out/fpga-flow"
                [bundle]
                schema_version = 3
                output_dir = "out/current"
                artifacts = { bitstream = "out/fpga-flow/artifacts/bitstream.bit", xsa = "out/fpga-flow/artifacts/hardware.xsa", fsbl = "out/fpga-flow/artifacts/fsbl.elf", app = "out/fpga-flow/artifacts/app.elf", boot_bin = "out/fpga-flow/artifacts/BOOT.BIN" }
                [toolchains.python]
                command = "python"
                [stages.hardware]
                kind = "build"
                outputs = ["out/fpga-flow/artifacts/bitstream.bit", "out/fpga-flow/artifacts/hardware.xsa"]
                command = ["python", "-c", "pass"]
                toolchains = ["python"]
                [stages.platform]
                kind = "build"
                needs = ["hardware"]
                outputs = ["out/fpga-flow/artifacts/fsbl.elf"]
                command = ["python", "-c", "pass"]
                toolchains = ["python"]
                [stages.app]
                kind = "build"
                needs = ["platform"]
                outputs = ["out/fpga-flow/artifacts/app.elf"]
                command = ["python", "-c", "pass"]
                toolchains = ["python"]
                [stages.boot]
                kind = "build"
                needs = ["app"]
                outputs = ["out/fpga-flow/artifacts/BOOT.BIN"]
                command = ["python", "-c", "pass"]
                toolchains = ["python"]
            """), encoding="utf-8")
            profile = load_profile(config)
            store = ManifestStore(root, profile.output_dir, profile.project_name)
            for stage, outputs in {
                "hardware": ["out/fpga-flow/artifacts/bitstream.bit", "out/fpga-flow/artifacts/hardware.xsa"],
                "platform": ["out/fpga-flow/artifacts/fsbl.elf"],
                "app": ["out/fpga-flow/artifacts/app.elf"],
                "boot": ["out/fpga-flow/artifacts/BOOT.BIN"],
            }.items():
                record = {
                    "stage": stage,
                    "kind": "build",
                    "status": "succeeded",
                    "outputs": {
                        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                        for path in outputs
                    },
                    "tool_versions": {"python": "python"},
                    "inputs": {},
                    "dependencies": {},
                    "evidence": {},
                }
                run_id = f"run-{stage}"
                store.record_run(run_id, stage, record)
            payload = refresh_bundle(profile, store)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["schema_version"], 3)
            self.assertTrue((root / "out/current/bundle.json").is_file())
            self.assertEqual(
                json.loads((root / "out/current/bundle.json").read_text())["artifacts"]["boot_bin"]["path"],
                "out/current/BOOT.BIN",
            )


if __name__ == "__main__":
    unittest.main()
