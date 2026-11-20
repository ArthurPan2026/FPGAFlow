"""Publish and validate a bundle composed from current Flow stage records."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from .fingerprint import FingerprintError, hash_artifact


class BundleError(ValueError):
    """A bundle cannot be constructed or does not match its source records."""


_FILENAMES = {
    "bitstream": "bitstream.bit", "xsa": "hardware.xsa", "hardware_xsa": "hardware.xsa",
    "fsbl": "fsbl.elf", "fsbl_elf": "fsbl.elf", "ps7_init": "ps7_init.tcl",
    "ps7_init_tcl": "ps7_init.tcl", "xpfm": "platform.xpfm", "platform": "platform.xpfm",
    "app": "app.elf", "app_elf": "app.elf", "application": "app.elf",
    "boot": "BOOT.BIN", "boot_bin": "BOOT.BIN", "boot.bin": "BOOT.BIN",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _checked_dir(root: Path, directory: Path) -> Path:
    root = root.resolve(strict=True)
    try:
        parts = directory.relative_to(root).parts
    except ValueError as exc:
        raise BundleError("bundle 输出目录必须位于项目内") from exc
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or (getattr(current, "is_junction", lambda: False)()):
            raise BundleError(f"bundle 输出目录包含链接：{current}")
        current.mkdir(exist_ok=True)
    return current


def artifact_producers(profile: Any) -> dict[str, str]:
    """Resolve each declared artifact to exactly one build-stage output."""
    bundle = getattr(profile, "bundle", None)
    if bundle is None:
        return {}
    owners: dict[str, str] = {}
    filenames: set[str] = set()
    for key, relative in bundle.artifacts.items():
        matches = [name for name, stage in profile.stages.items()
                   if stage.kind == "build" and any(
                       fnmatch.fnmatchcase(relative, pattern) for pattern in stage.outputs)]
        if len(matches) != 1:
            raise BundleError(f"bundle 产物 {key!r} 必须由唯一构建阶段声明，实际：{matches}")
        filename = _FILENAMES.get(key, Path(relative).name)
        if not filename or filename in filenames or filename == "bundle.json":
            raise BundleError(f"bundle 输出文件名冲突：{filename!r}")
        filenames.add(filename)
        owners[key] = matches[0]
    return owners


def _atomic_copy(source: Path, destination: Path, expected: str) -> None:
    if destination.is_symlink():
        raise BundleError(f"拒绝覆盖符号链接：{destination}")
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
            shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if _sha256(temporary) != expected:
            raise BundleError(f"复制时产物发生变化：{source}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise BundleError(f"拒绝覆盖符号链接：{path}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_bundle(profile: Any, records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Publish artifacts from stage records already validated by the Flow engine.

    The manifest is written last. Consumers must validate every referenced hash,
    so an interrupted copy fails closed instead of presenting a mixed bundle.
    """
    bundle = profile.bundle
    if bundle is None:
        raise BundleError("profile 未配置 bundle")
    owners = artifact_producers(profile)
    resolved: dict[str, tuple[str, str]] = {}
    for key, relative in bundle.artifacts.items():
        owner = owners[key]
        record = records.get(owner)
        outputs = record.get("outputs") if isinstance(record, Mapping) else None
        expected = outputs.get(relative) if isinstance(outputs, Mapping) else None
        if not isinstance(expected, str):
            raise BundleError(f"阶段 {owner!r} 没有记录 bundle 产物 {relative!r}")
        try:
            actual = hash_artifact(profile.root, relative)
        except (FingerprintError, OSError, ValueError) as exc:
            raise BundleError(f"无法验证 bundle 源产物 {relative!r}：{exc}") from exc
        if actual != expected:
            raise BundleError(f"bundle 源产物哈希不匹配：{relative}")
        resolved[key] = (owner, actual)

    profile_hash = _sha256(profile.config_path)
    identity = {
        "profile_sha256": profile_hash,
        "artifacts": {key: {"sha256": sha, "stage": owner,
                            "fingerprint": records[owner].get("fingerprint")}
                      for key, (owner, sha) in sorted(resolved.items())},
    }
    bundle_id = _json_sha256(identity)
    directory = _checked_dir(profile.root, bundle.output_dir)
    manifest_path = directory / "bundle.json"
    previous: Mapping[str, Any] = {}
    previous_evidence: Mapping[str, Any] = {}
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and old.get("bundle_id") == bundle_id:
                previous = old.get("verification", {}) if isinstance(old.get("verification"), dict) else {}
                previous_evidence = old.get("verification_evidence", {}) if isinstance(old.get("verification_evidence"), dict) else {}
        except (OSError, json.JSONDecodeError):
            pass

    artifacts: dict[str, dict[str, str]] = {}
    for key, (owner, digest) in sorted(resolved.items()):
        relative = bundle.artifacts[key]
        filename = _FILENAMES.get(key, Path(relative).name)
        destination = directory / filename
        _atomic_copy(profile.root / relative, destination, digest)
        artifacts[key] = {"path": destination.relative_to(profile.root).as_posix(),
                          "source": relative, "sha256": digest, "stage": owner,
                          "run_id": str(records[owner].get("run_id", "")),
                          "fingerprint": str(records[owner].get("fingerprint", ""))}
    toolchains: dict[str, str] = {}
    external: dict[str, dict[str, Any]] = {}
    for name, dependency in profile.external_dependencies.items():
        prefix = f"@external/{name}/"
        selected: dict[str, str] = {}
        for record in records.values():
            inputs = record.get("inputs", {})
            if isinstance(inputs, Mapping):
                for path, digest in inputs.items():
                    if isinstance(path, str) and path.startswith(prefix) and isinstance(digest, str):
                        selected[path[len(prefix):]] = digest
        if selected:
            external[name] = {"locked_revision": dependency.revision,
                              "selected_content_sha256": _json_sha256(selected),
                              "inputs": dict(sorted(selected.items()))}
    for record in records.values():
        values = record.get("tool_versions", {})
        if isinstance(values, Mapping):
            toolchains.update({str(k): str(v) for k, v in values.items()})
    payload: dict[str, Any] = {
        "schema_version": bundle.schema_version, "bundle_id": bundle_id,
        "profile_sha256": profile_hash, "artifacts": artifacts,
        "hardware": {k: v for k, v in artifacts.items() if k in {"bitstream", "xsa", "hardware_xsa"}},
        "software": {k: v for k, v in artifacts.items() if k not in {"bitstream", "xsa", "hardware_xsa"}},
        "dependencies": dict(bundle.dependencies), "toolchains": toolchains,
        "external_dependencies": external,
        "verification": {key: previous.get(key) is True for key in
                         ("jtag_deployed", "qspi_programmed", "qspi_readback_verified", "cold_boot_verified")},
        "verification_evidence": dict(previous_evidence),
    }
    _atomic_json(manifest_path, payload)
    return payload


def resolve_bundle_artifact(profile: Any, stage: Any, relative: str) -> Path:
    """Check a provider artifact is in the current bundle and has the recorded hash."""
    bundle = profile.bundle
    if bundle is None:
        raise BundleError("profile 未配置 bundle")
    path = bundle.output_dir / "bundle.json"
    if path.is_symlink():
        raise BundleError("bundle manifest 是符号链接")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"无法读取 bundle manifest：{exc}") from exc
    if not isinstance(data, dict) or data.get("profile_sha256") != _sha256(profile.config_path):
        raise BundleError("bundle profile 已变化")
    artifacts = data.get("artifacts")
    matches = [item for item in artifacts.values() if isinstance(item, dict) and item.get("path") == relative] if isinstance(artifacts, dict) else []
    if len(matches) != 1:
        raise BundleError(f"产物未被当前 bundle 唯一声明：{relative}")
    item = matches[0]
    needed: set[str] = set()
    pending = list(stage.needs)
    while pending:
        name = pending.pop()
        if name not in needed:
            needed.add(name)
            pending.extend(profile.stages[name].needs)
    if item.get("stage") not in needed:
        raise BundleError(f"部署阶段没有依赖产物所属阶段：{item.get('stage')}")
    try:
        actual = hash_artifact(profile.root, relative)
    except (FingerprintError, OSError, ValueError) as exc:
        raise BundleError(f"无法验证 bundle 产物：{exc}") from exc
    if actual != item.get("sha256"):
        raise BundleError(f"bundle 产物哈希不匹配：{relative}")
    return profile.root / relative


def record_bundle_deployment(profile: Any, bundle_id: str, runner: str,
                             *, verify: bool, run_id: str) -> None:
    """Bind a successful provider run to the bundle whose files were used."""
    bundle = profile.bundle
    if bundle is None:
        return
    path = bundle.output_dir / "bundle.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"无法读取已部署 bundle：{exc}") from exc
    if not isinstance(data, dict) or data.get("bundle_id") != bundle_id:
        raise BundleError("bundle 身份在部署期间发生变化")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BundleError("bundle 产物清单无效")
    for item in artifacts.values():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BundleError("bundle 产物记录无效")
        if hash_artifact(profile.root, item["path"]) != item.get("sha256"):
            raise BundleError(f"部署期间 bundle 产物发生变化：{item['path']}")
    verification = data.setdefault("verification", {})
    evidence = data.setdefault("verification_evidence", {})
    if not isinstance(verification, dict) or not isinstance(evidence, dict):
        raise BundleError("bundle 验证记录无效")
    if runner == "amd.qspi.program":
        verification.update({"qspi_programmed": True, "qspi_readback_verified": verify,
                             "cold_boot_verified": False})
        evidence.update({"qspi_programmed": run_id, "qspi_readback_verified": run_id if verify else None,
                         "cold_boot_verified": None})
    elif runner == "amd.zynq.jtag":
        verification["jtag_deployed"] = True
        evidence["jtag_deployed"] = run_id
    else:
        return
    _atomic_json(path, data)


def record_bundle_cold_boot(profile: Any, bundle_id: str, run_id: str) -> None:
    """Record an explicitly validated cold-boot report for the same bundle."""
    bundle = profile.bundle
    if bundle is None:
        raise BundleError("profile 未配置 bundle")
    path = bundle.output_dir / "bundle.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"无法读取 bundle：{exc}") from exc
    if not isinstance(data, dict) or data.get("bundle_id") != bundle_id:
        raise BundleError("冷启动报告与当前 bundle 不匹配")
    verification = data.get("verification")
    evidence = data.setdefault("verification_evidence", {})
    if not isinstance(verification, dict) or not isinstance(evidence, dict):
        raise BundleError("bundle 验证记录无效")
    if verification.get("qspi_programmed") is not True or verification.get("qspi_readback_verified") is not True:
        raise BundleError("当前 bundle 缺少 QSPI 读回验证")
    verification["cold_boot_verified"] = True
    evidence["cold_boot_verified"] = run_id
    _atomic_json(path, data)
