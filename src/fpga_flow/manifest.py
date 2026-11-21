"""项目状态索引与不可变运行记录。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RECENT_RUN_LIMIT = 50
_SUMMARY_KEYS = (
    "stage", "kind", "status", "target", "run_id", "fingerprint", "cache_hit",
    "started_at_utc", "completed_at_utc", "elapsed_seconds", "error_code", "error",
    "tool_versions", "last_success_run_id",
)


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


def _stage_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    summary = {key: record[key] for key in _SUMMARY_KEYS if key in record}
    for source, target in (("inputs", "input_count"), ("outputs", "output_count"), ("evidence", "evidence_count")):
        value = record.get(source, {})
        summary[target] = len(value) if isinstance(value, Mapping) else 0
    return summary


class ManifestStore:
    """manifest 只索引阶段摘要；完整证据保存在不可变 run.json 中。"""

    def __init__(
        self,
        project_root: Path,
        output_dir: str | Path,
        project_name: str = "",
        state_dir: str | Path | None = None,
    ) -> None:
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
        self.runs_dir = self.output_dir / "runs"
        if state_dir is None:
            self.state_dir = self.output_dir
        else:
            state_path = Path(state_dir).expanduser()
            if not state_path.is_absolute():
                raise ManifestError("Flow state directory must be absolute")
            self.state_dir = state_path.resolve(strict=False)
        self.manifest_path = self.state_dir / "manifest.json"
        self._legacy_manifest_path = self.output_dir / "manifest.json"
        self.project_name = project_name
        self._mutex = threading.RLock()

    @property
    def lock_path(self) -> Path:
        return self._safe_state_path("run.lock")

    def _safe_output_path(self, *parts: str) -> Path:
        relative = self._output_relative.joinpath(*parts)
        return _safe_project_path(self.project_root, relative, "Flow 输出路径")

    def _safe_state_path(self, *parts: str) -> Path:
        path = self.state_dir.joinpath(*parts)
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self.state_dir)
        except (OSError, ValueError) as exc:
            raise ManifestError(f"Flow state path escapes its state directory: {path}") from exc
        current = self.state_dir
        for part in Path(*parts).parts:
            current = current / part
            if current.is_symlink():
                raise ManifestError(f"refusing symlink in Flow state path: {current}")
        return resolved

    def empty(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {"name": self.project_name},
            "updated_at": utc_now(),
            "latest_run_id": None,
            "run_ids": [],
            "run_count": 0,
            "stages": {},
        }

    def load(self) -> dict[str, Any]:
        path = self._safe_state_path("manifest.json")
        if not path.is_file() and self.state_dir != self.output_dir and self._legacy_manifest_path.is_file():
            path = self._legacy_manifest_path
        if not path.exists():
            return self.empty()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"无法读取 manifest {path}：{exc}") from exc
        version = data.get("schema_version") if isinstance(data, dict) else None
        if version not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise ManifestError(f"manifest 版本不受支持：{path}")
        if not isinstance(data.get("stages"), dict) or not isinstance(data.get("run_ids"), list):
            raise ManifestError(f"manifest 结构无效：{path}")
        return data

    def _archive_legacy_manifest(self) -> None:
        source = self.manifest_path if self.state_dir == self.output_dir else self._legacy_manifest_path
        if not source.is_file():
            return
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        archive = (
            self._safe_output_path("runs", "migrations", f"manifest-v1-{digest[:16]}.json")
            if self.state_dir == self.output_dir
            else self._safe_state_path("migrations", f"manifest-{digest[:16]}.json")
        )
        if not archive.exists():
            _atomic_write(archive, raw, replace=False)

    def _compact_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        old_runs = manifest.get("run_ids", [])
        all_run_ids = list(old_runs) if isinstance(old_runs, list) else []
        run_ids = all_run_ids[-_RECENT_RUN_LIMIT:]
        old_run_count = int(manifest.get("run_count", len(all_run_ids)) or 0)
        old_stages = manifest.get("stages", {})
        stages: dict[str, dict[str, Any]] = {}
        if isinstance(old_stages, Mapping):
            for name, record in old_stages.items():
                if not isinstance(name, str) or not isinstance(record, Mapping):
                    continue
                summary = _stage_summary(record)
                if summary.get("status") == "succeeded":
                    summary["last_success_run_id"] = summary.get("run_id")
                else:
                    successful_id = None
                    for run_id in reversed(run_ids):
                        try:
                            candidate = self.read_run_record(run_id)
                        except ManifestError:
                            continue
                        if candidate.get("stage") == name and candidate.get("status") == "succeeded":
                            successful_id = run_id
                            break
                    summary["last_success_run_id"] = successful_id
                stages[name] = summary
        return {
            "schema_version": SCHEMA_VERSION,
            "project": dict(manifest.get("project", {})) if isinstance(manifest.get("project"), Mapping) else {},
            "updated_at": manifest.get("updated_at", utc_now()),
            "latest_run_id": manifest.get("latest_run_id"),
            "run_ids": run_ids,
            "run_count": max(len(all_run_ids), old_run_count),
            "stages": stages,
        }

    def save(self, manifest: Mapping[str, Any]) -> None:
        with self._mutex:
            data = dict(manifest)
            version = data.get("schema_version")
            if version == LEGACY_SCHEMA_VERSION:
                self._archive_legacy_manifest()
                data = self._compact_manifest(data)
            elif version != SCHEMA_VERSION:
                raise ManifestError("拒绝保存 schema_version 不受支持的 manifest")
            data["updated_at"] = utc_now()
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if self.state_dir != self.output_dir and not self.manifest_path.exists():
                self._archive_legacy_manifest()
            path = self._safe_state_path("manifest.json")
            _atomic_write(path, _json_bytes(data), replace=True)
            if self.state_dir != self.output_dir and self._legacy_manifest_path.is_file():
                self._legacy_manifest_path.unlink()

    def run_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ManifestError(f"运行标识不安全： {run_id!r}")
        return self._safe_output_path("runs", run_id, "run.json")

    def write_run_record(self, run_id: str, record: Mapping[str, Any]) -> Path:
        """写入不可变运行记录；已存在的 run_id 永不覆盖。"""
        with self._mutex:
            path = self.run_path(run_id)
            data = dict(record)
            data.setdefault("schema_version", SCHEMA_VERSION)
            data["run_id"] = run_id
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ManifestError("新运行记录必须使用当前 manifest 版本")
            _atomic_write(path, _json_bytes(data), replace=False)
            return path

    def read_run_record(self, run_id: str) -> dict[str, Any]:
        path = self.run_path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"无法读取运行记录 {run_id}：{exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("run_id") != run_id
            or data.get("schema_version") not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
        ):
            raise ManifestError(f"运行记录格式无效：{run_id}")
        return data

    def stage_record(self, manifest: Mapping[str, Any], stage_name: str) -> dict[str, Any] | None:
        stages = manifest.get("stages", {})
        summary = stages.get(stage_name) if isinstance(stages, Mapping) else None
        if not isinstance(summary, Mapping):
            return None
        run_id = summary.get("run_id")
        if manifest.get("schema_version") == SCHEMA_VERSION and isinstance(run_id, str):
            try:
                return self.read_run_record(run_id)
            except ManifestError:
                # running 状态在 run.json 写入前发布，需保留摘要供 resume 读取。
                if summary.get("status") == "running":
                    return dict(summary)
                raise
        return dict(summary)

    def update_stage(self, stage_name: str, summary: Mapping[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        if not isinstance(stage_name, str) or not _STAGE_ID.fullmatch(stage_name):
            raise ManifestError(f"阶段名称不安全： {stage_name!r}")
        with self._mutex:
            manifest = self.load()
            stages = dict(manifest.get("stages", {}))
            previous = stages.get(stage_name, {})
            stage_record = dict(summary)
            if stage_record.get("status") == "succeeded":
                stage_record["last_success_run_id"] = run_id or stage_record.get("run_id")
            else:
                last_success = previous.get("last_success_run_id") if isinstance(previous, Mapping) else None
                if not last_success and isinstance(previous, Mapping) and previous.get("status") == "succeeded":
                    last_success = previous.get("run_id")
                stage_record["last_success_run_id"] = last_success
            if run_id is not None:
                run_path = self.run_path(run_id)
                if not run_path.is_file():
                    raise ManifestError(f"必须先写入运行记录，再发布阶段指针：{run_id}")
                stage_record["run_id"] = run_id
                run_ids = list(manifest.get("run_ids", []))
                if run_id not in run_ids:
                    previous_count = int(manifest.get("run_count", len(run_ids)))
                    run_ids.append(run_id)
                    manifest["run_count"] = previous_count + 1
                manifest["run_ids"] = run_ids[-_RECENT_RUN_LIMIT:]
                manifest["latest_run_id"] = run_id
            stages[stage_name] = _stage_summary(stage_record)
            manifest["stages"] = stages
            self.save(manifest)
            return self.load()

    def record_run(self, run_id: str, stage_name: str, record: Mapping[str, Any]) -> Path:
        """先持久化运行记录，再原子更新该阶段的最新指针。"""
        with self._mutex:
            path = self.write_run_record(run_id, record)
            self.update_stage(stage_name, record, run_id=run_id)
            return path

    def latest_stage(self, stage_name: str) -> dict[str, Any] | None:
        return self.stage_record(self.load(), stage_name)

    def successful_stage_record(self, manifest: Mapping[str, Any], stage_name: str) -> dict[str, Any] | None:
        stages = manifest.get("stages", {})
        summary = stages.get(stage_name) if isinstance(stages, Mapping) else None
        if not isinstance(summary, Mapping):
            return None
        run_id = summary.get("last_success_run_id")
        if not run_id and summary.get("status") == "succeeded":
            run_id = summary.get("run_id")
        if not isinstance(run_id, str):
            return None
        try:
            return self.read_run_record(run_id)
        except ManifestError:
            return None

    def list_runs(self, *, limit: int = 20, before_run_id: str | None = None,
                  stage: str | None = None) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ManifestError("run history limit 必须是 1 到 500 之间的整数")
        if not self.runs_dir.exists():
            return []
        records = []
        for child in self.runs_dir.iterdir():
            if not child.is_dir() or not _RUN_ID.fullmatch(child.name):
                continue
            try:
                record = self.read_run_record(child.name)
            except ManifestError:
                continue
            if stage and record.get("stage") != stage:
                continue
            records.append(record)
        records.sort(key=lambda item: (
            str(item.get("started_at_utc", "")),
            str(item.get("completed_at_utc", "")),
            str(item.get("run_id", "")),
        ))
        if before_run_id is not None:
            index = next((i for i, item in enumerate(records)
                          if item.get("run_id") == before_run_id), None)
            if index is None:
                raise ManifestError(f"history cursor 不存在：{before_run_id}")
            records = records[:index]
        return list(reversed(records[-limit:]))
