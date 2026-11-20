"""Fingerprint explicitly declared Git dependencies outside a project root."""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any

from .fingerprint import FingerprintError, hash_inputs
from .models import ExternalDependencyConfig


class ExternalDependencyError(ValueError):
    """An external checkout is missing, dirty, or differs from its lock."""


def dependency_path(dependency: ExternalDependencyConfig) -> Path:
    value = os.environ.get(dependency.environment_variable, "") if dependency.environment_variable else ""
    return Path(value).expanduser().resolve() if value else dependency.path


def _git(root: Path, *arguments: str, allow_failure: bool = False) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalDependencyError(f"Git 依赖检查失败 {root}: {exc}") from exc
    if result.returncode and not allow_failure:
        raise ExternalDependencyError(f"Git 依赖检查失败 {root}: {result.stderr.decode(errors='replace').strip()}")
    return result


def inspect_dependency(dependency: ExternalDependencyConfig) -> dict[str, Any]:
    """Verify checkout identity and selected files, without modifying the checkout."""
    root = dependency_path(dependency)
    if not root.is_dir():
        raise ExternalDependencyError(f"外部依赖目录不存在：{dependency.name}: {root}")
    checkout = _git(root, "rev-parse", "--show-toplevel").stdout.decode(errors="replace").strip()
    if Path(checkout).resolve() != root:
        raise ExternalDependencyError(f"外部依赖路径不是 Git 根目录：{dependency.name}: {root}")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    if status:
        raise ExternalDependencyError(f"外部依赖工作树存在未提交改动：{dependency.name}: {root}")
    actual = _git(root, "rev-parse", "--verify", "HEAD").stdout.decode("ascii").strip().lower()
    changed: list[str] = []
    if actual != dependency.revision:
        ancestor = _git(root, "merge-base", "--is-ancestor", dependency.revision, actual, allow_failure=True)
        if ancestor.returncode != 0 or not dependency.ignored_committed_paths:
            raise ExternalDependencyError(f"外部依赖提交不匹配：{dependency.name}: expected {dependency.revision}, got {actual}")
        diff = _git(root, "diff", "--name-only", "--no-renames", "-z", dependency.revision, actual).stdout
        changed = [os.fsdecode(path) for path in diff.split(b"\0") if path]
        if any(not any(fnmatch.fnmatchcase(path, pattern) for pattern in dependency.ignored_committed_paths)
               for path in changed):
            raise ExternalDependencyError(f"外部依赖的代码或资源偏离锁定提交：{dependency.name}")
    try:
        inputs = hash_inputs(root, dependency.inputs, require_matches=True)
    except (FingerprintError, OSError, ValueError) as exc:
        raise ExternalDependencyError(f"无法计算外部依赖输入 {dependency.name}: {exc}") from exc
    return {"name": dependency.name, "path": str(root), "locked_revision": dependency.revision,
            "checkout_revision": actual, "ignored_committed_paths": changed, "inputs": inputs}


def stage_external_hashes(profile: Any, names: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in names:
        info = inspect_dependency(profile.external_dependencies[name])
        prefix = f"@external/{name}/"
        hashes[prefix + "locked_revision"] = hashlib.sha256(info["locked_revision"].encode("ascii")).hexdigest()
        hashes.update({prefix + path: digest for path, digest in info["inputs"].items()})
    return hashes
