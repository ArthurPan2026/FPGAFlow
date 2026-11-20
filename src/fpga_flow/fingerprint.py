"""为项目内 FPGA Flow 产物生成内容寻址指纹。

本模块不依赖 AMD 工具或具体项目的配置结构。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class FingerprintError(ValueError):
    """输入/输出路径或指纹内容不安全时抛出。"""


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    if path.is_symlink():
        raise FingerprintError(f"拒绝对符号链接计算哈希：{path}")
    if not path.is_file():
        raise FingerprintError(f"文件不存在：{path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FingerprintError("指纹映射的键必须是字符串")
        return {key: _jsonable(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    raise FingerprintError(f"不支持的指纹值类型：{type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, FingerprintError):
            raise
        raise FingerprintError(f"无法编码指纹 JSON：{exc}") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _root_path(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise FingerprintError(f"项目根目录不可用：{root}：{exc}") from exc
    if not resolved.is_dir():
        raise FingerprintError(f"项目根路径不是目录：{root}")
    return resolved


def _reject_symlink_components(base: Path, relative: Path, label: str) -> None:
    """拒绝路径中的所有符号链接，即使目标仍位于项目目录内。"""
    if relative.is_absolute() or ".." in relative.parts:
        raise FingerprintError(f"{label} 必须保持为项目相对路径：{relative}")
    current = base
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        try:
            if current.is_symlink():
                raise FingerprintError(f"拒绝 {label} 中的符号链接路径段：{current}")
        except OSError as exc:
            raise FingerprintError(f"无法检查 {label} 的路径段 {current}：{exc}") from exc


def _safe_relative(root: Path, value: str | Path, *, must_exist: bool = False) -> Path:
    base = _root_path(root)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise FingerprintError(f"路径必须相对于项目根目录，且不能包含 '..'：{value}")
    _reject_symlink_components(base, relative, "项目路径")
    candidate = base / relative
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise FingerprintError(f"无法解析项目路径 {value}：{exc}") from exc
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise FingerprintError(f"路径超出项目根目录：{value}") from exc
    return resolved


def _walk_regular_files(directory: Path, base: Path) -> list[Path]:
    result: list[Path] = []
    try:
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in sorted(directories):
                child = current_path / name
                if child.is_symlink():
                    raise FingerprintError(f"拒绝遍历目录符号链接：{child}")
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(files):
                child = current_path / name
                if child.is_symlink():
                    raise FingerprintError(f"拒绝对文件符号链接计算哈希：{child}")
                if child.is_file():
                    try:
                        child.relative_to(base)
                    except ValueError as exc:
                        raise FingerprintError(f"输入路径超出项目根目录：{child}") from exc
                    # The root has already been resolved, and os.walk rejects
                    # symlink directories and files. Re-resolving every leaf
                    # performs redundant realpath calls for large source trees.
                    result.append(child)
    except OSError as exc:
        raise FingerprintError(f"无法枚举目录 {directory}：{exc}") from exc
    return result


def expand_inputs(
    root: Path,
    patterns: Sequence[str | Path],
    *,
    require_matches: bool = True,
) -> list[Path]:
    """将相对 glob 模式展开为排序、去重后的文件列表。

    模式匹配到目录时会递归纳入其中的普通文件。符号链接
    会被拒绝，避免输入静默依赖项目目录之外的数据。
    """
    base = _root_path(root)
    selected: dict[str, Path] = {}
    covered_directories: set[Path] = set()
    for raw_pattern in patterns:
        pattern = str(raw_pattern)
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise FingerprintError(f"输入模式必须位于项目目录内：{pattern}")
        try:
            matches = sorted(base.glob(pattern), key=lambda path: (len(path.parts), path.as_posix()))
        except (OSError, ValueError) as exc:
            raise FingerprintError(f"输入 glob 模式 {pattern!r} 无效：{exc}") from exc
        if not matches and require_matches:
            raise FingerprintError(f"输入模式没有匹配到文件：{pattern}")
        for match in matches:
            if match.is_symlink():
                raise FingerprintError(f"拒绝使用符号链接作为输入：{match}")
            # A matched directory already contributes its entire subtree. Glob
            # patterns such as "src/**" also return every nested directory;
            # walking those again makes a large source tree quadratic in depth.
            if any(parent in covered_directories for parent in match.parents):
                continue
            resolved = _safe_relative(base, match.relative_to(base), must_exist=True)
            if resolved.is_file():
                selected[resolved.relative_to(base).as_posix()] = resolved
            elif resolved.is_dir():
                for item in _walk_regular_files(resolved, base):
                    selected[item.relative_to(base).as_posix()] = item
                covered_directories.add(match)
    return [selected[name] for name in sorted(selected)]


def hash_inputs(root: Path, patterns: Sequence[str | Path], *, require_matches: bool = True) -> dict[str, str]:
    """返回所选输入文件的“项目相对路径 -> SHA-256”映射。"""
    base = _root_path(root)
    paths = expand_inputs(base, patterns, require_matches=require_matches)
    return {path.relative_to(base).as_posix(): sha256_file(path) for path in paths}


def hash_artifact(root: Path, artifact: str | Path) -> str:
    """对一个项目内文件或目录产物确定性地计算哈希。"""
    base = _root_path(root)
    path = _safe_relative(base, artifact, must_exist=True)
    if path.is_symlink():
        raise FingerprintError(f"拒绝对产物符号链接计算哈希：{path}")
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        raise FingerprintError(f"产物既不是文件也不是目录：{artifact}")
    entries: list[dict[str, str]] = []
    try:
        for current, directories, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            for name in sorted(directories):
                child = current_path / name
                if child.is_symlink():
                    raise FingerprintError(f"拒绝对目录符号链接计算哈希：{child}")
                entries.append({"path": child.relative_to(path).as_posix(), "type": "directory"})
            for name in sorted(files):
                child = current_path / name
                if child.is_symlink():
                    raise FingerprintError(f"拒绝对文件符号链接计算哈希：{child}")
                if child.is_file():
                    entries.append({
                        "path": child.relative_to(path).as_posix(),
                        "type": "file",
                        "sha256": sha256_file(child),
                    })
    except OSError as exc:
        raise FingerprintError(f"无法对目录产物 {artifact} 计算哈希：{exc}") from exc
    entries.sort(key=lambda item: (item["path"], item["type"]))
    return sha256_json({"kind": "directory", "entries": entries})


def hash_artifacts(root: Path, artifacts: Iterable[str | Path]) -> dict[str, str]:
    """按项目相对路径为必需的文件和目录产物计算哈希。"""
    base = _root_path(root)
    result: dict[str, str] = {}
    for artifact in artifacts:
        name = Path(artifact).as_posix()
        if name in result:
            continue
        result[name] = hash_artifact(base, artifact)
    return {name: result[name] for name in sorted(result)}


def stage_fingerprint(
    stage: Mapping[str, Any],
    input_hashes: Mapping[str, str],
    dependency_output_hashes: Mapping[str, Any],
    tool_versions: Mapping[str, str],
) -> str:
    """根据阶段契约和所有上游标识计算稳定的阶段指纹。"""
    payload = {
        "stage": stage,
        "inputs": input_hashes,
        "dependencies": dependency_output_hashes,
        "tool_versions": tool_versions,
    }
    return sha256_json(payload)


def cache_record_valid(
    record: Mapping[str, Any],
    fingerprint: str,
    required_outputs: Sequence[str | Path],
    root: Path,
) -> bool:
    """判断之前的成功运行是否具有相同指纹且产物完好。

    所有已记录产物都会重新计算哈希；每个必需路径都必须出现在
    记录中。无效或不安全的记录一律视为缓存未命中。
    """
    if record.get("status") != "succeeded" or record.get("fingerprint") != fingerprint:
        return False
    outputs = record.get("outputs")
    if not isinstance(outputs, Mapping) or not outputs:
        return False
    required = {Path(path).as_posix() for path in required_outputs}
    if not required.issubset({str(name) for name in outputs}):
        return False
    try:
        actual = hash_artifacts(root, [str(name) for name in outputs])
    except (FingerprintError, OSError, ValueError):
        return False
    return all(
        isinstance(expected, str) and actual.get(str(name)) == expected
        for name, expected in outputs.items()
    ) and set(actual) == set(str(name) for name in outputs)
