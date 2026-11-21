"""Publish selected verified bundle artifacts into a new delivery directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from .fingerprint import hash_artifact


def export_bundle(profile: Any, bundle: Mapping[str, Any]) -> Path | None:
    project = profile.project_descriptor
    if project is None or project.export_dir is None:
        return None
    artifacts = bundle["artifacts"]
    unknown = set(project.export_artifacts) - set(artifacts)
    if unknown:
        raise ValueError(f"unknown export artifacts: {sorted(unknown)}")
    parent = profile.root / project.export_dir
    current = profile.root
    for part in Path(project.export_dir).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError(f"export directory contains a link: {current}")
    parent.resolve(strict=False).relative_to(profile.root.resolve())
    parent.mkdir(parents=True, exist_ok=True)
    run_ids = sorted({artifacts[key]["run_id"] for key in project.export_artifacts})
    name = run_ids[-1] + "-" + bundle["bundle_id"][:12]
    destination = parent / name
    if destination.is_symlink():
        raise ValueError(f"export destination is a link: {destination}")
    manifest = {"bundle_id": bundle["bundle_id"], "artifacts": {}}
    for key in project.export_artifacts:
        item = artifacts[key]
        source = item["source"]
        if hash_artifact(profile.root, source) != item["sha256"]:
            raise ValueError(f"export source changed: {source}")
        manifest["artifacts"][key] = {"filename": Path(item["path"]).name,
            "sha256": item["sha256"], "run_id": item["run_id"]}
    if destination.exists():
        if json.loads((destination / "delivery.json").read_text(encoding="utf-8")) != manifest:
            raise ValueError(f"refusing to overwrite delivery: {destination}")
        for item in manifest["artifacts"].values():
            relative = (destination / item["filename"]).relative_to(profile.root).as_posix()
            if hash_artifact(profile.root, relative) != item["sha256"]:
                raise ValueError(f"existing delivery changed: {relative}")
        return destination
    temporary = Path(tempfile.mkdtemp(prefix=".delivery-", dir=parent))
    try:
        for key, item in manifest["artifacts"].items():
            source = profile.root / artifacts[key]["source"]
            copied = temporary / item["filename"]
            shutil.copyfile(source, copied)
            if hash_artifact(profile.root, copied.relative_to(profile.root).as_posix()) != item["sha256"]:
                raise ValueError(f"artifact changed during export: {source}")
        (temporary / "delivery.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
