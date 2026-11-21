"""Verify legacy hardware evidence before importing it into a project profile."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from .fingerprint import hash_artifact, sha256_file, stage_fingerprint
from .project import project_stage_inputs


def migrate_hardware(engine: Any, legacy_run: Path, proof_path: Path) -> Mapping[str, Any]:
    # Local import avoids a cycle with the command dispatcher.
    from .engine import _new_run_id, _stage_contract, _stage_input_hashes, _write_json
    from .manifest import utc_now
    profile = engine.profile
    if profile.project_descriptor is None or "hardware" not in profile.stages:
        raise ValueError("hardware migration requires a board project descriptor")
    old = json.loads(legacy_run.read_text(encoding="utf-8"))
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    if old.get("stage") != "hardware" or old.get("status") != "succeeded":
        raise ValueError("legacy record is not a successful hardware run")
    if proof.get("legacy_record_sha256") != sha256_file(legacy_run):
        raise ValueError("migration proof does not identify this legacy record")
    if not proof.get("review") or proof.get("hardware_configuration_equivalent") is not True:
        raise ValueError("explicit hardware configuration equivalence review is required")
    target = profile.default_target
    if old.get("target") != target:
        raise ValueError("legacy board target does not match")
    for group in ("outputs", "evidence"):
        if not old.get(group):
            raise ValueError(f"legacy {group} are missing")
        for relative, digest in old[group].items():
            if hash_artifact(profile.root, relative) != digest:
                raise ValueError(f"legacy {group} changed: {relative}")
    equivalent = proof.get("equivalent_inputs", {})
    for relative in project_stage_inputs(profile.project_descriptor, "hardware"):
        digest = sha256_file(profile.root / relative)
        if old["inputs"].get(relative) == digest:
            continue
        item = equivalent.get(relative, {})
        if item.get("sha256") != digest or not item.get("reason") or not item.get("derived_from"):
            raise ValueError(f"hardware input has no legacy coverage: {relative}")
        for dependency in item["derived_from"]:
            if old["inputs"].get(dependency) != sha256_file(profile.root / dependency):
                raise ValueError(f"derivation input changed: {dependency}")
    stage = profile.stages["hardware"]
    with engine._mutation_guard("migrate-hardware"):
        run_id = _new_run_id()
        run_dir = engine.store.run_path(run_id).parent
        run_dir.mkdir(parents=True, exist_ok=False)
        versions, details = engine._probe_stage_toolchains(stage, run_dir)
        for name in stage.toolchains:
            if versions[name] != old["tool_versions"].get(name):
                raise ValueError(f"legacy toolchain differs: {name}")
        outputs = {}
        prefix = profile.output_dir.relative_to(profile.root).as_posix()
        work = run_dir / "work"
        source_map = proof.get("outputs", {})
        for logical in stage.outputs:
            source = source_map.get(logical)
            if source not in old["outputs"]:
                raise ValueError(f"migration output is not legacy evidence: {logical}")
            relative = logical.removeprefix(prefix + "/")
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(profile.root / source, destination)
            if sha256_file(destination) != old["outputs"][source]:
                raise ValueError(f"migration copy changed: {source}")
            outputs[destination.relative_to(profile.root).as_posix()] = old["outputs"][source]
        # A passing gate must itself be a verified legacy output.
        gate = next((work / logical.removeprefix(prefix + "/") for logical in stage.outputs
                     if logical.endswith("/qor_gate.tsv")), None)
        if gate is None:
            raise ValueError("migration requires a legacy QoR/DRC gate")
        values = dict(line.split("=", 1) for line in gate.read_text().splitlines() if "=" in line)
        if not (values.get("passed") == "1" and float(values["wns_ns"]) >= 0
                and float(values["whs_ns"]) >= 0 and float(values["tns_ns"]) == 0
                and float(values["ths_ns"]) == 0 and int(values["drc_errors"]) == 0
                and int(values["critical_warnings"]) == 0):
            raise ValueError("legacy QoR/DRC gate did not pass")
        inputs = _stage_input_hashes(profile, stage)
        fingerprint = stage_fingerprint(_stage_contract(profile, stage, target), inputs, {}, versions)
        _write_json(run_dir / "migration-proof.json", proof)
        _write_json(run_dir / "legacy-run.json", old)
        (run_dir / "stdout.log").write_text("Verified legacy hardware migration; synthesis was not run.\n")
        (run_dir / "stderr.log").write_text("")
        _write_json(run_dir / "command.json", {"action": "migrate-hardware", "legacy_run": str(legacy_run)})
        _write_json(run_dir / "execution.json", {"succeeded": True, "migration": True})
        return engine._complete_run(stage, run_id, "succeeded", utc_now(), target=target,
            inputs=inputs, dependencies={}, tool_versions=versions, toolchain_details=details,
            fingerprint=fingerprint, outputs=outputs)
