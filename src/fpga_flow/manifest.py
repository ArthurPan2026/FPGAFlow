"""FPGA Flow 的原子化 manifest 和不可变运行记录。

manifest 保存各阶段的最新状态索引。详细运行记录
以不可变文件保存，Agent 可在上下文丢失后据此恢复。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ManifestError(ValueError):
    """manifest 或运行标识格式错误、不安全时抛出。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reject_symlink_components(base: Path, relative: Path, label: str) -> None:
    """拒绝路径中的符号链接，即使目标仍位于项目目录内。"""
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError(f"{label} 必须保持为项目相对路径：{relative}")
    current = base
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        try:
            if current.is_symlink():
                raise ManifestError(f"拒绝 {label} 中的符号链接路径段：{current}")
        except OSError as exc:
            raise ManifestError(f"无法检查 {label} 的路径段 {current}：{exc}") from exc


def _safe_project_path(project_root: Path, relative: str | Path, label: str) -> Path:
    rel = Path(relative)
    _reject_symlink_components(project_root, rel, label)
    candidate = project_root / rel
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{label} 超出项目根目录或不可用：{relative}") from exc
    return resolved


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"manifest 记录无法序列化为 JSON：{exc}") from exc


def _atomic_write(path: Path, contents: bytes, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp_path, path)
        else:
            # 硬链接创建是原子的；若不可变目标已存在，操作会失败。
            os.link(temp_path, path)
            temp_path.unlink()
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


class ManifestStore:
    """项目内的 manifest：当前状态原子更新，运行记录以不可变文件保存。"""

    def __init__(self, project_root: Path, output_dir: str | Path, project_name: str = "") -> None:
        try:
            self.project_root = Path(project_root).resolve(strict=True)
        except OSError as exc:
            raise ManifestError(f"项目根目录不可用：{project_root}：{exc}") from exc
        if not self.project_root.is_dir():
            raise ManifestError(f"项目根路径不是目录：{project_root}")
        output_path = Path(output_dir)
        candidate = output_path if output_path.is_absolute() else self.project_root / output_path
        try:
            output_relative = candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise ManifestError(f"Flow 输出目录必须位于项目根目录内：{output_dir}") from exc
        if ".." in output_relative.parts:
            raise ManifestError(f"输出目录不能包含 '..'：{output_dir}")
        self._output_relative = output_relative
        self.output_dir = _safe_project_path(self.project_root, output_relative, "Flow 输出目录")
        self.manifest_path = self.output_dir / "manifest.json"
        self.runs_dir = self.output_dir / "runs"
        self.project_name = project_name

    def _safe_output_path(self, *parts: str) -> Path:
        relative = self._output_relative.joinpath(*parts)
        return _safe_project_path(self.project_root, relative, "Flow 输出路径")

    def empty(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {"name": self.project_name},
            "updated_at": utc_now(),
            "latest_run_id": None,
            "run_ids": [],
            "stages": {},
        }

    def load(self) -> dict[str, Any]:
        path = self._safe_output_path("manifest.json")
        if not path.exists():
            return self.empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"无法读取 manifest {path}：{exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestError(f"manifest 版本不受支持：{path}")
        if not isinstance(data.get("stages"), dict) or not isinstance(data.get("run_ids"), list):
            raise ManifestError(f"manifest 结构无效：{path}")
        return data

    def save(self, manifest: Mapping[str, Any]) -> None:
        data = dict(manifest)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestError("拒绝保存 schema_version 不受支持的 manifest")
        data["updated_at"] = utc_now()
        output_dir = self._safe_output_path()
        output_dir.mkdir(parents=True, exist_ok=True)
        path = self._safe_output_path("manifest.json")
        _atomic_write(path, _json_bytes(data), replace=True)

    def run_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ManifestError(f"运行标识不安全： {run_id!r}")
        return self._safe_output_path("runs", run_id, "run.json")

    def write_run_record(self, run_id: str, record: Mapping[str, Any]) -> Path:
        """写入不可变运行记录；已存在的 run_id 永不覆盖。"""
        path = self.run_path(run_id)
        data = dict(record)
        data.setdefault("schema_version", SCHEMA_VERSION)
        data["run_id"] = run_id
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestError("运行记录的 schema_version 与当前 Flow 版本不匹配")
        _atomic_write(path, _json_bytes(data), replace=False)
        return path

    def read_run_record(self, run_id: str) -> dict[str, Any]:
        path = self.run_path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"无法读取运行记录 {run_id}：{exc}") from exc
        if not isinstance(data, dict) or data.get("run_id") != run_id or data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestError(f"运行记录格式无效：{run_id}")
        return data

    def update_stage(self, stage_name: str, summary: Mapping[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        if not isinstance(stage_name, str) or not _STAGE_ID.fullmatch(stage_name):
            raise ManifestError(f"阶段名称不安全： {stage_name!r}")
        manifest = self.load()
        stages = dict(manifest.get("stages", {}))
        stage_record = dict(summary)
        if run_id is not None:
            # 发布阶段指针前，先确认对应的不可变证据文件已写入。
            run_path = self.run_path(run_id)
            if not run_path.is_file():
                raise ManifestError(f"必须先写入运行记录，再发布阶段指针：{run_id}")
            stage_record["run_id"] = run_id
            run_ids = list(manifest.get("run_ids", []))
            if run_id not in run_ids:
                run_ids.append(run_id)
            manifest["run_ids"] = run_ids
            manifest["latest_run_id"] = run_id
        stages[stage_name] = stage_record
        manifest["stages"] = stages
        self.save(manifest)
        return manifest

    def record_run(self, run_id: str, stage_name: str, record: Mapping[str, Any]) -> Path:
        """先持久化运行记录，再原子更新该阶段的最新指针。"""
        path = self.write_run_record(run_id, record)
        self.update_stage(stage_name, record, run_id=run_id)
        return path

    def latest_stage(self, stage_name: str) -> dict[str, Any] | None:
        manifest = self.load()
        value = manifest.get("stages", {}).get(stage_name)
        return dict(value) if isinstance(value, dict) else None
