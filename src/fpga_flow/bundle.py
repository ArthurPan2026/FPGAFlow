"""Materialize a verified, content-addressed bundle for a Flow profile."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_for_key(key: str) -> str:
    normalized = key.lower()
    if normalized in {"bitstream", "xsa", "hardware_xsa"}:
        return "hardware"
    if normalized in {"fsbl", "fsbl_elf", "ps7_init", "ps7_init_tcl", "xpfm", "platform"}:
        return "platform"
    if normalized in {"app", "app_elf", "application"}:
        return "app"
    if normalized in {"boot", "boot_bin", "boot.bin"}:
        return "boot"
    return normalized


def refresh_bundle(profile: Any, store: Any) -> dict[str, Any] | None:
    """Refresh out/current once every declared artifact is current and verified.

    Profiles without a bundle section are unaffected. A missing or stale
    upstream stage returns None and leaves the previous bundle untouched.
    """
    bundle = getattr(profile, "bundle", None)
    if bundle is None:
        return None
    manifest = store.load()
    resolved: dict[str, tuple[str, Path, str]] = {}
    for key, relative in bundle.artifacts.items():
        stage_name = _stage_for_key(key)
        record = store.successful_stage_record(manifest, stage_name)
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            return None
        source = profile.root / relative
        if source.is_symlink() or not source.is_file():
            return None
        outputs = record.get("outputs")
        expected = outputs.get(relative) if isinstance(outputs, Mapping) else None
        actual = _sha256(source)
        if not isinstance(expected, str) or expected != actual:
            return None
        resolved[key] = (stage_name, source, actual)

    output_dir = bundle.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    published: dict[str, dict[str, str]] = {}
    hardware: dict[str, dict[str, str]] = {}
    software: dict[str, dict[str, str]] = {}
    filename_map = {
        "bitstream": "bitstream.bit",
        "xsa": "hardware.xsa",
        "hardware_xsa": "hardware.xsa",
        "fsbl": "fsbl.elf",
        "fsbl_elf": "fsbl.elf",
        "ps7_init": "ps7_init.tcl",
        "ps7_init_tcl": "ps7_init.tcl",
        "xpfm": "platform.xpfm",
        "platform": "platform.xpfm",
        "app": "app.elf",
        "app_elf": "app.elf",
        "application": "app.elf",
        "boot": "BOOT.BIN",
        "boot_bin": "BOOT.BIN",
        "boot.bin": "BOOT.BIN",
    }
    for key, (stage_name, source, sha256) in sorted(resolved.items()):
        filename = filename_map.get(key, Path(bundle.artifacts[key]).name)
        destination = output_dir / filename
        _atomic_copy(source, destination)
        item = {
            "path": destination.relative_to(profile.root).as_posix(),
            "source": bundle.artifacts[key],
            "sha256": sha256,
            "stage": stage_name,
        }
        published[key] = item
        (hardware if stage_name == "hardware" else software)[key] = item

    old_path = output_dir / "bundle.json"
    old_verification: dict[str, Any] = {}
    if old_path.is_file():
        try:
            old = json.loads(old_path.read_text(encoding="utf-8"))
            if isinstance(old, Mapping) and isinstance(old.get("verification"), Mapping):
                old_verification = dict(old["verification"])
        except (OSError, json.JSONDecodeError):
            pass

    toolchains: dict[str, str] = {}
    for stage_name in ("hardware", "platform", "app", "boot"):
        record = store.successful_stage_record(manifest, stage_name)
        values = record.get("tool_versions", {}) if isinstance(record, Mapping) else {}
        if isinstance(values, Mapping):
            for name, value in values.items():
                if isinstance(name, str) and isinstance(value, str):
                    toolchains[name] = value

    profile_sha256 = _sha256(profile.config_path)
    bundle_id = _json_sha256({
        "profile_sha256": profile_sha256,
        "artifacts": {key: item["sha256"] for key, item in sorted(published.items())},
    })
    payload = {
        "schema_version": int(bundle.schema_version),
        "bundle_id": bundle_id,
        "profile_sha256": profile_sha256,
        "hardware": hardware,
        "software": software,
        "artifacts": published,
        "dependencies": dict(bundle.dependencies),
        "toolchains": toolchains,
        "verification": {
            "jtag_deployed": bool(old_verification.get("jtag_deployed", False)),
            "qspi_programmed": bool(old_verification.get("qspi_programmed", False)),
            "qspi_readback_verified": bool(old_verification.get("qspi_readback_verified", False)),
            "cold_boot_verified": bool(old_verification.get("cold_boot_verified", False)),
        },
    }
    temporary = output_dir / ".bundle.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, old_path)
    return payload
