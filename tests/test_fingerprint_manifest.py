from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fpga_flow.fingerprint import (
    FingerprintError,
    cache_record_valid,
    hash_artifact,
    hash_artifacts,
    hash_inputs,
    sha256_file,
    sha256_json,
    stage_fingerprint,
)
from fpga_flow.manifest import ManifestError, ManifestStore


class FingerprintTests(unittest.TestCase):
    def test_file_and_json_hashes_are_content_sensitive_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_path = root / "src" / "top.sv"
            file_path.parent.mkdir()
            file_path.write_text("module top; endmodule\n", encoding="utf-8")
            first = sha256_file(file_path)
            self.assertEqual(first, sha256_file(file_path))
            self.assertEqual(sha256_json({"b": 2, "a": 1}), sha256_json({"a": 1, "b": 2}))
            file_path.write_text("module top; wire x; endmodule\n", encoding="utf-8")
            self.assertNotEqual(first, sha256_file(file_path))

    def test_input_globs_are_sorted_deduplicated_and_project_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative, content in (("src/rtl/b.sv", "b"), ("src/rtl/a.sv", "a")):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            hashes = hash_inputs(root, ["src/rtl/*.sv", "src/rtl/a.sv"])
            self.assertEqual(list(hashes), ["src/rtl/a.sv", "src/rtl/b.sv"])
            self.assertEqual(hashes["src/rtl/a.sv"], sha256_file(root / "src/rtl/a.sv"))

    def test_input_glob_rejects_escape_unmatched_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            with self.assertRaises(FingerprintError):
                hash_inputs(root, ["../outside/*.sv"])
            with self.assertRaises(FingerprintError):
                hash_inputs(root, ["src/missing/*.sv"])
            source = Path(outside) / "secret.sv"
            source.write_text("module secret; endmodule\n", encoding="utf-8")
            try:
                (root / "linked.sv").symlink_to(source)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(FingerprintError):
                hash_inputs(root, ["linked.sv"])

    def test_directory_artifact_hash_covers_relative_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "out" / "platform.xpfm"
            (artifact / "data").mkdir(parents=True)
            (artifact / "data" / "domain.yaml").write_text("domain: freertos\n", encoding="utf-8")
            first = hash_artifact(root, "out/platform.xpfm")
            self.assertEqual(first, hash_artifact(root, "out/platform.xpfm"))
            (artifact / "data" / "domain.yaml").write_text("domain: standalone\n", encoding="utf-8")
            self.assertNotEqual(first, hash_artifact(root, "out/platform.xpfm"))

    def test_internal_symlink_components_are_rejected_for_inputs_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "src" / "real" / "top.sv"
            target.parent.mkdir(parents=True)
            target.write_text("module top; endmodule\n", encoding="utf-8")
            alias = root / "src" / "alias"
            try:
                alias.symlink_to(target.parent, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(FingerprintError):
                hash_inputs(root, ["src/alias/top.sv"])
            with self.assertRaises(FingerprintError):
                hash_artifact(root, "src/alias/top.sv")

    def test_artifact_hash_rejects_symlinks_and_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / "out").mkdir()
            secret = Path(outside) / "secret.bin"
            secret.write_bytes(b"secret")
            try:
                (root / "out" / "external.bin").symlink_to(secret)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(FingerprintError):
                hash_artifact(root, "out/external.bin")
            with self.assertRaises(FingerprintError):
                hash_artifact(root, "../outside/secret.bin")

    def test_stage_fingerprint_tracks_stage_inputs_dependencies_and_tool_versions(self) -> None:
        stage = {"kind": "build", "command": ["vivado", "-mode", "batch"]}
        base = stage_fingerprint(stage, {"src/top.sv": "a" * 64}, {"xsa": "b" * 64}, {"vivado": "2025.2.1"})
        self.assertEqual(base, stage_fingerprint(stage, {"src/top.sv": "a" * 64}, {"xsa": "b" * 64}, {"vivado": "2025.2.1"}))
        self.assertNotEqual(base, stage_fingerprint(stage, {"src/top.sv": "c" * 64}, {"xsa": "b" * 64}, {"vivado": "2025.2.1"}))
        self.assertNotEqual(base, stage_fingerprint(stage, {"src/top.sv": "a" * 64}, {"xsa": "d" * 64}, {"vivado": "2025.2.1"}))
        self.assertNotEqual(base, stage_fingerprint(stage, {"src/top.sv": "a" * 64}, {"xsa": "b" * 64}, {"vivado": "2025.2.2"}))

    def test_cache_hit_requires_same_fingerprint_and_all_output_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out" / "design.bit"
            output.parent.mkdir()
            output.write_bytes(b"bitstream")
            hashes = hash_artifacts(root, ["out/design.bit"])
            record = {"status": "succeeded", "fingerprint": "fingerprint-1", "outputs": hashes}
            self.assertTrue(cache_record_valid(record, "fingerprint-1", ["out/design.bit"], root))
            self.assertFalse(cache_record_valid(record, "fingerprint-2", ["out/design.bit"], root))
            self.assertFalse(cache_record_valid(record, "fingerprint-1", ["out/other.bit"], root))
            output.write_bytes(b"corrupted")
            self.assertFalse(cache_record_valid(record, "fingerprint-1", ["out/design.bit"], root))


class ManifestStoreTests(unittest.TestCase):
    def test_manifest_is_atomic_index_and_run_records_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManifestStore(root, "out/fpga-flow", "sample-amd-project")
            manifest = store.load()
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["project"]["name"], "sample-amd-project")
            run_id = "20261115T000000Z-abcd1234"
            record = {
                "stage": "hardware",
                "status": "succeeded",
                "fingerprint": "a" * 64,
                "outputs": {"out/design.bit": "b" * 64},
            }
            path = store.record_run(run_id, "hardware", record)
            self.assertTrue(path.is_file())
            self.assertEqual(store.read_run_record(run_id)["stage"], "hardware")
            loaded = store.load()
            self.assertEqual(loaded["latest_run_id"], run_id)
            self.assertEqual(loaded["stages"]["hardware"]["run_id"], run_id)
            self.assertIn(run_id, loaded["run_ids"])
            with self.assertRaises(FileExistsError):
                store.write_run_record(run_id, record)
            with self.assertRaises(ManifestError):
                store.run_path("../../escape")

    def test_output_dir_and_run_records_reject_internal_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "out" / "real-flow").mkdir(parents=True)
            try:
                (root / "out" / "flow-alias").symlink_to(root / "out" / "real-flow", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(ManifestError):
                ManifestStore(root, "out/flow-alias", "demo")

            store = ManifestStore(root, "out/fpga-flow", "demo")
            store.output_dir.mkdir(parents=True, exist_ok=True)
            alternate_runs = store.output_dir / "alternate-runs"
            alternate_runs.mkdir()
            (store.output_dir / "runs").symlink_to(alternate_runs, target_is_directory=True)
            with self.assertRaises(ManifestError):
                store.run_path("run-1")

    def test_manifest_file_symlink_is_rejected_even_when_target_is_internal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManifestStore(root, "out/fpga-flow", "demo")
            store.output_dir.mkdir(parents=True, exist_ok=True)
            target = store.output_dir / "other.json"
            target.write_text('{"schema_version":1,"run_ids":[],"stages":{}}\n', encoding="utf-8")
            try:
                store.manifest_path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaises(ManifestError):
                store.load()

    def test_manifest_rejects_unsupported_schema_and_keeps_old_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ManifestStore(root, "out/fpga-flow", "demo")
            first = {"stage": "hardware", "status": "failed", "error": "synth"}
            second = {"stage": "hardware", "status": "succeeded", "fingerprint": "new"}
            store.record_run("run-1", "hardware", first)
            store.record_run("run-2", "hardware", second)
            manifest = store.load()
            self.assertEqual(manifest["run_ids"], ["run-1", "run-2"])
            self.assertEqual(store.read_run_record("run-1")["status"], "failed")
            self.assertEqual(manifest["stages"]["hardware"]["run_id"], "run-2")
            with self.assertRaises(ManifestError):
                store.save({"schema_version": 999, "stages": {}, "run_ids": []})


if __name__ == "__main__":
    unittest.main()
