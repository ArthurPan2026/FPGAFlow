"""面向不同 AMD FPGA 项目的构建、验证与板级部署编排引擎。"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid

from .config import ProfileError, topological_order
from .bundle import BundleError, artifact_producers, refresh_bundle
from .fingerprint import (
    FingerprintError,
    expand_inputs,
    hash_artifacts,
    hash_inputs,
    sha256_file,
    sha256_json,
    stage_fingerprint,
)
from .manifest import ManifestError, ManifestStore, SCHEMA_VERSION, utc_now
from .locking import ProjectRunLock, RunLockError
from .models import FlowConfig, StageConfig
from .runner import CommandResult, run_command
from .toolchains import ToolchainResult, probe_toolchain


class FlowError(RuntimeError):
    """Flow 操作失败时使用的稳定、机器可读异常。"""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StageProvider(Protocol):
    """AMD 工具及板卡操作提供器的接口。"""
    def execute(self, stage: StageConfig, profile: FlowConfig, run_dir: Path, target: str | None) -> CommandResult: ...


_PROVIDER_REGISTRY: dict[str, StageProvider] = {}


def register_provider(name: str, provider: StageProvider) -> None:
    if not name or name == "command":
        raise ValueError("提供器名称不能为空，也不能覆盖内置的 command runner")
    _PROVIDER_REGISTRY[name] = provider


def clear_providers_for_tests() -> None:
    _PROVIDER_REGISTRY.clear()


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]


def _runner_name(stage: StageConfig) -> str:
    return str(getattr(stage, "runner", "command") or "command")


def _stage_options(stage: StageConfig) -> Mapping[str, Any]:
    value = getattr(stage, "options", {})
    return value if isinstance(value, Mapping) else {}


def _target_profile_fingerprint(profile: FlowConfig, stage: StageConfig, target: str | None) -> dict[str, Any]:
    if target is None:
        return {}
    target_profile = profile.targets.get(target, {})
    if not isinstance(target_profile, Mapping):
        return {}
    explicit = target_profile.get("hardware_identity")
    if isinstance(explicit, Mapping):
        identity = dict(explicit)
    else:
        identity_keys = {"family", "part", "board_part", "device", "board_id"}
        identity = {key: target_profile[key] for key in sorted(identity_keys) if key in target_profile}
    runner = _runner_name(stage)
    if stage.kind in {"build", "test"}:
        return {"hardware_identity": identity}
    transport_keys: set[str] | None = None
    if runner == "amd.zynq.jtag":
        transport_keys = {
            "jtag", "jtag_target", "ps_target_pattern", "pl_target_pattern", "cpu_target_pattern",
            "hw_server_url", "server_url", "ps_init_commands", "run_ms",
        }
    elif runner == "amd.qspi.program":
        transport_keys = {
            "qspi", "flash_type", "offset", "flash_size_bytes", "target_device_name",
            "target_name_contains", "target_idcode", "server_url", "hw_server_url",
        }
    elif runner == "amd.uart.commands":
        transport_keys = {
            "uart", "serial", "serial_number", "serial_id", "vid", "pid", "interface", "port",
            "baud", "baudrate", "prompt_regex", "line_ending", "timeout_seconds",
            "write_timeout_seconds", "idle_seconds",
        }
    if transport_keys is not None:
        transport = {key: target_profile[key] for key in sorted(transport_keys) if key in target_profile}
        return {"hardware_identity": identity, "transport_profile": transport}
    # 自定义运行时检查或部署命令可能会使用 profile 中的任意数据。
    return {"hardware_identity": identity, "target_profile": dict(target_profile)}


def _stage_contract(profile: FlowConfig, stage: StageConfig, target: str | None) -> dict[str, Any]:
    try:
        cwd = stage.cwd.relative_to(profile.root).as_posix()
    except ValueError:
        cwd = str(stage.cwd)
    return {
        "name": stage.name, "kind": stage.kind, "runner": _runner_name(stage),
        "options": dict(_stage_options(stage)), "needs": list(stage.needs),
        "inputs": list(stage.inputs), "outputs": list(stage.outputs),
        "command": list(stage.command), "cwd": cwd,
        "toolchains": list(stage.toolchains),
        "toolchain_locks": {name: profile.toolchains[name].expected_version for name in stage.toolchains},
        "toolchain_specs": {
            name: {
                "command": list(profile.toolchains[name].command),
                "version_command": list(profile.toolchains[name].version_command)
                if getattr(profile.toolchains[name], "version_command", None) is not None else None,
                "version_args": list(profile.toolchains[name].version_args),
                "expected_version": profile.toolchains[name].expected_version,
            }
            for name in stage.toolchains
        },
        "env": dict(stage.env), "timeout_seconds": stage.timeout_seconds,
        "success_markers": list(stage.success_markers),
        "failure_markers": list(stage.failure_markers),
        "target": stage.target, "target_context": target,
        "target_profile": _target_profile_fingerprint(profile, stage, target),
        "destructive": stage.destructive,
    }


def _format_value(value: str, fields: Mapping[str, str], where: str) -> str:
    allowed = {"root", "out_dir", "run_dir", "target"}
    try:
        for _, field, _, _ in Formatter().parse(value):
            if field is not None and field not in allowed:
                raise FlowError("FLOW-PLACEHOLDER", f"未知占位符 {{{field}}}（位置：{where}）")
        return value.format_map(fields)
    except (KeyError, ValueError) as exc:
        if isinstance(exc, FlowError):
            raise
        raise FlowError("FLOW-PLACEHOLDER", f"{where} 中的占位符无效：{exc}") from exc


def _format_argv(argv: Sequence[str], fields: Mapping[str, str]) -> tuple[str, ...]:
    if not argv:
        raise FlowError("FLOW-COMMAND-EMPTY", "命令执行器需要非空 argv")
    return tuple(_format_value(item, fields, f"argv[{index}]") for index, item in enumerate(argv))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_failure_logs(run_dir: Path, command: Sequence[str], message: str, started: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stdout.log").write_text("", encoding="utf-8")
    (run_dir / "stderr.log").write_text(message + "\n", encoding="utf-8")
    _write_json(run_dir / "command.json", {"argv": list(command), "started_without_runner": True})
    _write_json(run_dir / "execution.json", {"started_at_utc": started, "completed_at_utc": utc_now(),
                                               "exit_code": 1, "timed_out": False, "succeeded": False,
                                               "error": message})


def _write_provider_logs(run_dir: Path, stage: StageConfig, target: str | None, result: CommandResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    _write_json(run_dir / "command.json", {"argv": list(result.command), "runner": _runner_name(stage),
                                            "stage": stage.name, "target": target, "cwd": str(result.cwd)})
    _write_json(run_dir / "execution.json", {"started_at_utc": result.started_at_utc,
                                              "completed_at_utc": result.completed_at_utc,
                                              "elapsed_seconds": result.elapsed_seconds,
                                              "exit_code": result.exit_code, "timed_out": result.timed_out,
                                              "succeeded": result.succeeded, "error": result.error})


def _tool_version_value(result: ToolchainResult) -> str:
    return f"{result.resolved_command or ''}\n{result.reported_version or ''}"


def _expand_output_patterns(root: Path, patterns: Sequence[str]) -> tuple[list[str], dict[str, list[str]]]:
    """将每个必需输出模式解析为项目内的具体产物路径。"""
    base = root.resolve(strict=True)
    concrete: set[str] = set()
    by_pattern: dict[str, list[str]] = {}
    for pattern in patterns:
        rel = Path(pattern)
        if rel.is_absolute() or ".." in rel.parts or chr(92) in pattern:
            raise FlowError("FLOW-OUTPUT-PATH", f"输出模式不安全：{pattern!r}")
        try:
            matches = list(base.glob(pattern))
        except (OSError, ValueError) as exc:
            raise FlowError("FLOW-OUTPUT-PATH", f"输出模式 {pattern!r} 无效：{exc}") from exc
        names: list[str] = []
        for match in matches:
            try:
                name = match.relative_to(base).as_posix()
            except ValueError as exc:
                raise FlowError("FLOW-OUTPUT-PATH", f"输出路径超出项目根目录：{pattern!r}") from exc
            names.append(name)
            concrete.add(name)
        if not names:
            raise FlowError("FLOW-OUTPUT-MISSING", f"必需输出模式没有匹配到产物：{pattern}")
        by_pattern[pattern] = sorted(set(names))
    return sorted(concrete), by_pattern


def _outputs_match(record: Mapping[str, Any], fingerprint: str, patterns: Sequence[str], root: Path) -> bool:
    if record.get("status") != "succeeded" or record.get("fingerprint") != fingerprint:
        return False
    expected = record.get("outputs")
    if not isinstance(expected, Mapping):
        return False
    try:
        concrete, per_pattern = _expand_output_patterns(root, patterns)
        if any(not matches for matches in per_pattern.values()):
            return False
        if set(concrete) != {str(name) for name in expected}:
            return False
        actual = hash_artifacts(root, concrete)
    except (FlowError, FingerprintError, OSError, ValueError):
        return False
    return all(isinstance(value, str) and actual.get(str(name)) == value for name, value in expected.items())


def _evidence_hashes(root: Path, run_dir: Path) -> dict[str, str]:
    """对运行中的所有证据文件计算哈希，但不包含不可变 run.json 本身。"""
    relative = run_dir.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
    files = expand_inputs(root, [f"{relative}/**"], require_matches=False)
    selected = [path.relative_to(root.resolve()).as_posix() for path in files if path.name != "run.json"]
    return hash_artifacts(root, selected) if selected else {}


def _evidence_status(root: Path, record: Mapping[str, Any] | None, store: ManifestStore) -> str:
    if not isinstance(record, Mapping):
        return "missing"
    evidence = record.get("evidence")
    run_id = record.get("run_id")
    if not isinstance(evidence, Mapping) or not isinstance(run_id, str):
        return "unrecorded"
    try:
        run_dir = store.run_path(run_id).parent
        current = _evidence_hashes(root, run_dir)
    except (ManifestError, FingerprintError, OSError, ValueError):
        return "tampered-or-missing"
    if current == dict(evidence):
        return "verified"
    return "tampered-or-missing"


def _file_contains(path: Path, marker: str) -> bool:
    needle = marker.encode("utf-8")
    overlap = b""
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return False
                data = overlap + chunk
                if needle in data:
                    return True
                overlap = data[-max(0, len(needle) - 1):] if len(needle) > 1 else b""
    except OSError:
        return False


def _marker_in_logs(run_dir: Path, marker: str, fallback: str) -> bool:
    if marker in fallback:
        return True
    for path in run_dir.rglob("*"):
        if path.is_file() and path.name in {"stdout.log", "stderr.log"} and _file_contains(path, marker):
            return True
    return False


def _output_patterns_overlap(left: str, right: str) -> bool:
    def split(pattern: str) -> tuple[tuple[str, ...], bool]:
        parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part)
        prefix = []
        wildcard = False
        for part in parts:
            if any(char in part for char in "*?[]"):
                wildcard = True
                break
            prefix.append(part)
        return tuple(prefix if wildcard else parts), wildcard

    left_base, left_wild = split(left)
    right_base, right_wild = split(right)
    if not left_wild and not right_wild:
        return (left_base == right_base
                or left_base == right_base[:len(left_base)]
                or right_base == left_base[:len(right_base)])
    shorter, longer = (left_base, right_base) if len(left_base) <= len(right_base) else (right_base, left_base)
    return longer[:len(shorter)] == shorter


class FlowEngine:
    """按项目定义的阶段图执行任务，并记录缓存与运行来源。"""
    def __init__(self, profile: FlowConfig, *, providers: Mapping[str, StageProvider] | None = None,
                 progress_callback: Callable[[Mapping[str, Any]], None] | None = None):
        self.profile = profile
        self.store = ManifestStore(profile.root, profile.output_dir, profile.project_name)
        self.providers = dict(_PROVIDER_REGISTRY)
        if providers:
            self.providers.update(providers)
        self.progress_callback = progress_callback
        self._active_run_lock: ProjectRunLock | None = None
        self._wait_lock_current = False
        self._completed_in_session: set[str] = set()
        self._session_lock = threading.RLock()

    @classmethod
    def from_file(cls, config_path: str | Path = "fpga-flow.toml", *,
                  providers: Mapping[str, StageProvider] | None = None,
                  progress_callback: Callable[[Mapping[str, Any]], None] | None = None) -> "FlowEngine":
        from .config import load_profile
        return cls(load_profile(config_path), providers=providers, progress_callback=progress_callback)

    def _notify(self, event: Mapping[str, Any]) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(dict(event))
        except Exception:
            pass

    @contextmanager
    def _mutation_guard(self, action: str, *, wait_lock: bool = False) -> Iterator[ProjectRunLock]:
        lock = ProjectRunLock(self.store.lock_path, action=action, wait=wait_lock)
        try:
            lock.__enter__()
        except RunLockError as exc:
            raise FlowError("FLOW-LOCK-BUSY", str(exc)) from exc
        previous_wait = self._wait_lock_current
        self._active_run_lock = lock
        self._wait_lock_current = wait_lock
        try:
            yield lock
        finally:
            self._wait_lock_current = previous_wait
            self._active_run_lock = None
            lock.__exit__(None, None, None)

    def _target_resource_lock_path(self, target: str | None) -> Path | None:
        if target is None:
            return None
        profile = self.profile.targets.get(target)
        if not isinstance(profile, Mapping):
            return None
        identity = profile.get("hardware_identity")
        if isinstance(identity, Mapping) and identity:
            stable = dict(identity)
        else:
            stable = {
                key: profile[key] for key in ("board_id", "serial_number", "jtag_id", "target_id", "device_id")
                if key in profile
            }
            for section_name in ("uart", "jtag"):
                section = profile.get(section_name)
                if isinstance(section, Mapping):
                    keys = ("serial_number", "serial_id", "port", "vid", "pid", "interface",
                            "target_id", "idcode", "device_id")
                    values = {key: section[key] for key in keys if key in section}
                    if values:
                        stable[section_name] = values
        if not stable:
            return None
        digest = sha256_json(stable)
        override = os.environ.get("FPGA_FLOW_LOCK_DIR")
        if override:
            root = Path(override).expanduser()
        elif os.name == "nt":
            root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "FPGAFlow" / "target-locks"
        else:
            root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "fpga-flow" / "target-locks"
        return root / f"{digest}.lock"

    @contextmanager
    def _stage_target_lock(self, stage: StageConfig, target: str | None, run_id: str) -> Iterator[None]:
        if stage.kind not in {"deploy", "validate"}:
            yield
            return
        lock_path = self._target_resource_lock_path(stage.target or target)
        if lock_path is None:
            yield
            return
        lock = ProjectRunLock(lock_path, action=f"{stage.kind}:{stage.name}", wait=self._wait_lock_current)
        try:
            with lock:
                lock.set_stage(stage.name, run_id)
                lock.set_run(run_id)
                try:
                    yield
                finally:
                    lock.clear_stage(run_id)
        except RunLockError as exc:
            raise FlowError("FLOW-TARGET-LOCK-BUSY", str(exc)) from exc

    def _check_side_effects(self, stages: Sequence[str], allow_side_effects: bool) -> None:
        required = [(name, self.profile.stages[name].side_effects) for name in stages
                    if self.profile.stages[name].side_effects]
        if required and not allow_side_effects:
            detail = "; ".join(f"{name}={','.join(effects)}" for name, effects in required)
            raise FlowError("FLOW-SIDE-EFFECT-ACK",
                            "运行这些阶段前必须显式传入 --allow-side-effects：" + detail)

    def _target(self, requested: str | None, stage: StageConfig | None = None) -> str | None:
        selected = requested or (stage.target if stage else None) or self.profile.default_target
        if requested is not None and requested not in self.profile.targets:
            raise FlowError("FLOW-TARGET-UNKNOWN", f"未知目标 profile：{requested}")
        if selected is not None and selected not in self.profile.targets:
            raise FlowError("FLOW-TARGET-UNKNOWN", f"未知目标 profile：{selected}")
        if stage and stage.target and requested and stage.target != requested:
            raise FlowError("FLOW-TARGET-MISMATCH", f"阶段 {stage.name!r} 固定使用目标 {stage.target!r}，不能改用 {requested!r}")
        return selected

    def _toolchain_command(self, name: str) -> tuple[str, tuple[str, ...]]:
        config = self.profile.toolchains[name]
        if config.version_command:
            return config.version_command[0], tuple(config.version_command[1:])
        return config.command[0], (*config.command[1:], *config.version_args)

    def _probe_stage_toolchains(self, stage: StageConfig, run_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        versions: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        for name in stage.toolchains:
            config = self.profile.toolchains[name]
            command, version_args = self._toolchain_command(name)
            probe = probe_toolchain(name, command, version_args, config.expected_version,
                                    cwd=self.profile.root, log_dir=run_dir / "toolchains" / name,
                                    failure_markers=config.failure_markers)
            detail = {"name": probe.name, "configured_command": probe.configured_command,
                      "resolved_command": probe.resolved_command, "expected_version": probe.expected_version,
                      "reported_version": probe.reported_version, "status": probe.status,
                      "detail": probe.detail, "unlocked": config.expected_version is None}
            details[name] = detail
            if not probe.ok:
                raise FlowError("FLOW-TOOLCHAIN", f"工具链 {name!r}：{probe.detail}")
            versions[name] = _tool_version_value(probe)
        return versions, details

    def _probe_all_toolchains(
        self, run_dir: Path, names: Sequence[str] | None = None
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]], bool]:
        versions: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        okay = True
        selected = list(names) if names is not None else list(self.profile.toolchains)
        for name in dict.fromkeys(selected):
            config = self.profile.toolchains.get(name)
            if config is None:
                details[name] = {"name": name, "status": "missing",
                                 "detail": "阶段引用了未配置的工具链", "unlocked": False}
                okay = False
                continue
            command, version_args = self._toolchain_command(name)
            probe = probe_toolchain(
                name, command, version_args, config.expected_version,
                cwd=self.profile.root, log_dir=run_dir / "toolchains" / name,
                failure_markers=config.failure_markers,
            )
            details[name] = {
                "name": probe.name, "configured_command": probe.configured_command,
                "resolved_command": probe.resolved_command, "expected_version": probe.expected_version,
                "reported_version": probe.reported_version, "status": probe.status,
                "detail": probe.detail, "unlocked": config.expected_version is None,
            }
            if probe.ok:
                versions[name] = _tool_version_value(probe)
            elif probe.status != "warning":
                okay = False
        return versions, details, okay

    def _tool_versions_from_record(self, stage: StageConfig, record: Mapping[str, Any]) -> dict[str, str] | None:
        versions = record.get("tool_versions", {})
        details = record.get("toolchain_details", {})
        if not isinstance(versions, Mapping) or not isinstance(details, Mapping):
            return None
        result: dict[str, str] = {}
        for name in stage.toolchains:
            version = versions.get(name)
            detail = details.get(name)
            if not isinstance(version, str) or not isinstance(detail, Mapping):
                return None
            expected = self.profile.toolchains[name].expected_version
            reported = detail.get("reported_version")
            if expected is not None and (not isinstance(reported, str) or expected not in reported):
                return None
            result[name] = version
        return result

    def _dependency_hashes(
        self,
        stage: StageConfig,
        manifest: Mapping[str, Any],
        memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]],
        stack: set[str],
        target: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        hashes: dict[str, Any] = {}
        for name in stage.needs:
            dependency = self.profile.stages[name]
            dependency_target = dependency.target or target
            valid, reason, record = self._cache_state(name, manifest, memo, stack, dependency_target)
            if not valid or record is None:
                return None, f"dependency {name!r} is stale: {reason}"
            outputs = record.get("outputs", {})
            if not isinstance(outputs, Mapping):
                return None, f"依赖阶段 {name!r} 的产物哈希格式无效"
            hashes[name] = {"fingerprint": record.get("fingerprint"), "outputs": dict(outputs)}
        return hashes, None

    def _cache_state(
        self,
        stage_name: str,
        manifest: Mapping[str, Any] | None = None,
        memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]] | None = None,
        stack: set[str] | None = None,
        target: str | None = None,
    ) -> tuple[bool, str, Mapping[str, Any] | None]:
        memo = memo if memo is not None else {}
        stack = stack if stack is not None else set()
        if stage_name in stack:
            return False, "依赖关系存在循环", None
        stage = self.profile.stages.get(stage_name)
        if stage is None:
            return False, "profile 中没有该阶段", None
        if manifest is None:
            manifest = self.store.load()
        stages = manifest.get("stages", {})
        summary = stages.get(stage_name) if isinstance(stages, Mapping) else None
        context_target = (stage.target or target
                          or (summary.get("target") if isinstance(summary, Mapping) else None)
                          or self.profile.default_target)
        memo_key = (stage_name, context_target)
        if memo_key in memo:
            return memo[memo_key]
        if stage.kind == "validate" and stage_name not in self._completed_in_session:
            result = (False, "运行时/板级验证是证据，不可作为可复用缓存命中",
                      summary if isinstance(summary, Mapping) else None)
            memo[memo_key] = result
            return result
        if not isinstance(summary, Mapping):
            result = (False, "没有成功的阶段运行记录", None)
            memo[memo_key] = result
            return result
        try:
            record = self.store.successful_stage_record(manifest, stage_name)
        except ManifestError as exc:
            result = (False, f"运行记录缺失或损坏：{exc}", dict(summary))
            memo[memo_key] = result
            return result
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            result = (False, "没有可复用的成功阶段运行记录", dict(summary))
            memo[memo_key] = result
            return result
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            result = (False, "不可变运行记录不存在或状态不是成功", summary)
            memo[memo_key] = result
            return result
        versions = self._tool_versions_from_record(stage, record)
        if versions is None:
            result = (False, "工具链证据缺失或不符合配置的版本锁", record)
            memo[memo_key] = result
            return result
        stack.add(stage_name)
        try:
            dependency_hashes, error = self._dependency_hashes(stage, manifest, memo, stack, context_target)
        finally:
            stack.remove(stage_name)
        if dependency_hashes is None:
            result = (False, error or "依赖阶段已过期", record)
            memo[memo_key] = result
            return result
        try:
            inputs = hash_inputs(self.profile.root, stage.inputs, require_matches=bool(stage.inputs))
            fingerprint = stage_fingerprint(
                _stage_contract(self.profile, stage, context_target),
                inputs, dependency_hashes, versions,
            )
            if record.get("fingerprint") != fingerprint:
                result = (False, "输入、依赖、工具链、目标或阶段配置已改变", record)
            elif stage.outputs and not _outputs_match(record, fingerprint, stage.outputs, self.profile.root):
                result = (False, "一个或多个必需产物缺失或哈希无效", record)
            elif not stage.outputs and record.get("outputs", {}) != {}:
                result = (False, "无输出阶段却记录了产物", record)
            else:
                result = (True, "输入指纹与产物哈希均匹配", record)
        except (FlowError, FingerprintError, ManifestError, OSError, ValueError) as exc:
            result = (False, f"无法验证阶段产物：{exc}", record)
        memo[memo_key] = result
        return result

    def _require_dependencies(self, stage: StageConfig, target: str | None) -> None:
        manifest = self.store.load()
        memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]] = {}
        for name in stage.needs:
            if name in self._completed_in_session:
                continue
            valid, reason, _ = self._cache_state(name, manifest, memo, set(), target)
            if not valid:
                raise FlowError("FLOW-DEPENDENCY-STALE",
                                f"阶段 {stage.name!r} 需要有效的前置阶段 {name!r}：{reason}；请先构建前置阶段，或使用 --with-deps")

    def _check_runner(self, stage: StageConfig) -> None:
        runner = _runner_name(stage)
        if runner == "command":
            if not stage.command:
                raise FlowError("FLOW-COMMAND-EMPTY", f"命令阶段 {stage.name!r} 未配置 argv")
            return
        if runner not in self.providers:
            raise FlowError("FLOW-PROVIDER-UNSUPPORTED",
                            f"阶段 {stage.name!r} 的 runner={runner!r} 不可用；不会回退到 shell 执行")

    def _dispatch(self, stage: StageConfig, run_dir: Path, target: str | None,
                  dependencies: Mapping[str, Any], versions: Mapping[str, str]) -> tuple[CommandResult, tuple[str, ...]]:
        runner = _runner_name(stage)
        if runner == "command":
            fields = {"root": str(self.profile.root), "out_dir": str(self.profile.output_dir),
                      "run_dir": str(run_dir), "target": target or ""}
            command = _format_argv(stage.command, fields)
            try:
                cwd = stage.cwd.resolve(strict=True)
                cwd.relative_to(self.profile.root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise FlowError("FLOW-CWD-INVALID", f"阶段工作目录超出项目根目录或不存在：{stage.cwd}") from exc
            env = {key: _format_value(value, fields, f"stages.{stage.name}.env.{key}")
                   for key, value in stage.env.items()}
            # Long-running project-owned test runners can bind each result to
            # exactly the dependency and toolchain records verified by Flow.
            env["FPGA_FLOW_DEPENDENCIES_JSON"] = json.dumps(dependencies, sort_keys=True)
            env["FPGA_FLOW_TOOL_VERSIONS_JSON"] = json.dumps(versions, sort_keys=True)
            def progress(stats: Mapping[str, object]) -> None:
                self._notify({"event": "stage_progress", "stage": stage.name,
                              "run_id": run_dir.name, **dict(stats)})
            result = run_command(command, cwd=cwd, env=env,
                                 timeout_seconds=stage.timeout_seconds, log_dir=run_dir,
                                 on_progress=progress)
            return result, command
        provider = self.providers[runner]
        result = provider.execute(stage, self.profile, run_dir, target)
        if not isinstance(result, CommandResult):
            raise FlowError("FLOW-PROVIDER-RESULT", f"runner {runner!r} 返回了无效的 CommandResult")
        _write_provider_logs(run_dir, stage, target, result)
        return result, result.command

    def _complete_run(
        self,
        stage: StageConfig,
        run_id: str,
        status: str,
        started: str,
        *,
        target: str | None,
        inputs: Mapping[str, str],
        dependencies: Mapping[str, Any],
        tool_versions: Mapping[str, str],
        toolchain_details: Mapping[str, Any],
        fingerprint: str | None,
        outputs: Mapping[str, str],
        command: Sequence[str] = (),
        cache_hit: bool = False,
        error: FlowError | None = None,
        elapsed_seconds: float | None = None,
    ) -> dict[str, Any]:
        run_dir = self.store.run_path(run_id).parent
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "stage": stage.name, "kind": stage.kind,
            "status": status, "target": target, "fingerprint": fingerprint,
            "inputs": dict(inputs), "dependencies": dict(dependencies),
            "tool_versions": dict(tool_versions), "toolchain_details": dict(toolchain_details),
            "outputs": dict(outputs), "command": list(command), "cache_hit": cache_hit,
            "started_at_utc": started, "completed_at_utc": utc_now(),
            "log_files": {"stdout": "stdout.log", "stderr": "stderr.log",
                          "command": "command.json", "execution": "execution.json"},
        }
        if elapsed_seconds is not None:
            record["elapsed_seconds"] = elapsed_seconds
        if error is not None:
            record["error_code"] = error.code
            record["error"] = str(error)
        try:
            record["evidence"] = _evidence_hashes(self.profile.root, run_dir)
        except (FingerprintError, ManifestError, OSError, ValueError) as exc:
            if status == "succeeded":
                raise FlowError("FLOW-EVIDENCE-HASH", f"无法为运行证据计算哈希：{exc}") from exc
            record["evidence"] = {}
            record["evidence_hash_error"] = str(exc)
        self.store.record_run(run_id, stage.name, record)
        if self._active_run_lock is not None:
            self._active_run_lock.clear_stage(run_id)
        if status == "succeeded":
            with self._session_lock:
                self._completed_in_session.add(stage.name)
        self._notify({"event": "stage_finished", "stage": stage.name, "run_id": run_id,
                      "status": status, "elapsed_seconds": elapsed_seconds,
                      "error_code": record.get("error_code")})
        return record

    def _execute_stage(self, stage: StageConfig, *, target: str | None = None,
                       force: bool = False, allow_side_effects: bool = False) -> dict[str, Any]:
        self._check_side_effects((stage.name,), allow_side_effects)
        self._check_runner(stage)
        selected_target = self._target(target, stage)
        if stage.kind == "deploy" and target is None:
            raise FlowError("FLOW-TARGET-REQUIRED", f"部署阶段 {stage.name!r} 必须显式指定 --target")
        self._require_dependencies(stage, selected_target)
        pre_manifest = self.store.load()
        previous_stage_record = self.store.successful_stage_record(pre_manifest, stage.name) or {}
        run_id = _new_run_id()
        if self._active_run_lock is not None:
            self._active_run_lock.set_stage(stage.name, run_id)
            self._active_run_lock.set_run(run_id)
        run_dir = self.store.run_path(run_id).parent
        started = utc_now()
        inputs: dict[str, str] = {}
        dependencies: dict[str, Any] = {}
        versions: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        fingerprint: str | None = None
        command_preview = tuple(stage.command)
        if _runner_name(stage) == "command" and stage.command:
            fields = {"root": str(self.profile.root), "out_dir": str(self.profile.output_dir),
                      "run_dir": str(run_dir), "target": selected_target or ""}
            command_preview = _format_argv(stage.command, fields)
        command_result: CommandResult | None = None
        run_dir.mkdir(parents=True, exist_ok=False)
        self.store.update_stage(stage.name, {
            "stage": stage.name, "kind": stage.kind, "status": "running", "run_id": run_id,
            "target": selected_target, "started_at_utc": started, "fingerprint": None, "outputs": {},
        })
        self._notify({"event": "stage_started", "stage": stage.name, "run_id": run_id,
                      "target": selected_target, "kind": stage.kind})
        try:
            inputs = hash_inputs(self.profile.root, stage.inputs, require_matches=bool(stage.inputs))
            versions, details = self._probe_stage_toolchains(stage, run_dir)
            manifest = self.store.load()
            dependencies, dependency_error = self._dependency_hashes(
                stage, manifest, {}, set(), selected_target
            )
            if dependencies is None:
                raise FlowError("FLOW-DEPENDENCY-STALE", dependency_error or "依赖阶段已过期")
            fingerprint = stage_fingerprint(
                _stage_contract(self.profile, stage, selected_target),
                inputs, dependencies, versions,
            )
            cacheable = stage.kind in {"build", "test"} and not force
            if cacheable and isinstance(previous_stage_record, Mapping) and _outputs_match(
                previous_stage_record, fingerprint, stage.outputs, self.profile.root
            ):
                outputs = dict(previous_stage_record.get("outputs", {}))
                (run_dir / "stdout.log").write_text("", encoding="utf-8")
                (run_dir / "stderr.log").write_text("", encoding="utf-8")
                _write_json(run_dir / "command.json", {
                    "cache_hit": True, "stage": stage.name,
                    "target": selected_target, "argv": list(command_preview),
                })
                _write_json(run_dir / "execution.json", {
                    "succeeded": True, "cache_hit": True,
                    "started_at_utc": started, "completed_at_utc": utc_now(),
                })
                return self._complete_run(
                    stage, run_id, "succeeded", started, target=selected_target,
                    inputs=inputs, dependencies=dependencies, tool_versions=versions,
                    toolchain_details=details, fingerprint=fingerprint, outputs=outputs,
                    command=command_preview, cache_hit=True, elapsed_seconds=0.0,
                )

            with self._stage_target_lock(stage, selected_target, run_id):
                if stage.kind == "deploy" and self.profile.bundle is not None:
                    self._refresh_bundle(selected_target, required=True)
                command_result, command_preview = self._dispatch(stage, run_dir, selected_target, dependencies, versions)
            output_text = command_result.stdout + "\n" + command_result.stderr
            if not command_result.succeeded:
                raise FlowError(
                    "FLOW-COMMAND-FAILED",
                    command_result.error or f"命令退出码为 {command_result.exit_code}",
                )
            failed = [marker for marker in stage.failure_markers
                      if _marker_in_logs(run_dir, marker, output_text)]
            if failed:
                raise FlowError("FLOW-FAILURE-MARKER", f"检测到失败标记：{failed}")
            missing = [marker for marker in stage.success_markers
                       if not _marker_in_logs(run_dir, marker, output_text)]
            if missing:
                raise FlowError("FLOW-SUCCESS-MARKER-MISSING", f"缺少成功标记：{missing}")
            post_inputs = hash_inputs(self.profile.root, stage.inputs, require_matches=bool(stage.inputs))
            if post_inputs != inputs:
                raise FlowError("FLOW-INPUT-CHANGED", "阶段运行期间输入发生变化，拒绝记录成功")
            post_dependencies, dependency_error = self._dependency_hashes(
                stage, self.store.load(), {}, set(), selected_target
            )
            if post_dependencies is None or post_dependencies != dependencies:
                raise FlowError("FLOW-DEPENDENCY-CHANGED", dependency_error or "阶段运行期间依赖发生变化")
            try:
                concrete_outputs, _ = _expand_output_patterns(self.profile.root, stage.outputs)
                outputs = hash_artifacts(self.profile.root, concrete_outputs)
            except (FlowError, FingerprintError, OSError, ValueError) as exc:
                raise FlowError("FLOW-OUTPUT-INVALID", str(exc)) from exc
            return self._complete_run(
                stage, run_id, "succeeded", started, target=selected_target,
                inputs=inputs, dependencies=dependencies, tool_versions=versions,
                toolchain_details=details, fingerprint=fingerprint, outputs=outputs,
                command=command_preview, elapsed_seconds=command_result.elapsed_seconds,
            )
        except Exception as exc:
            error = exc if isinstance(exc, FlowError) else FlowError(
                "FLOW-STAGE-FAILED", f"{type(exc).__name__}：{exc}"
            )
            if not (run_dir / "stderr.log").is_file() or not (run_dir / "command.json").is_file():
                _write_failure_logs(run_dir, command_preview, f"{error.code}: {error}", started)
            (run_dir / "flow-error.txt").write_text(f"{error.code}: {error}\n", encoding="utf-8")
            self._complete_run(
                stage, run_id, "failed", started, target=selected_target,
                inputs=inputs, dependencies=dependencies, tool_versions=versions,
                toolchain_details=details, fingerprint=fingerprint, outputs={},
                command=command_preview, error=error,
                elapsed_seconds=command_result.elapsed_seconds if command_result else None,
            )
            raise error from exc

    def _require_cached(self, stage_name: str, target: str | None = None) -> Mapping[str, Any]:
        valid, reason, record = self._cache_state(stage_name, target=target)
        if not valid or record is None:
            raise FlowError("FLOW-DEPENDENCY-STALE", f"必需阶段 {stage_name!r} 已过期：{reason}")
        return record

    def _refresh_bundle(self, target: str | None, *, required: bool) -> dict[str, Any] | None:
        """Publish only when every producer and its full dependency chain are current."""
        if self.profile.bundle is None:
            return None
        try:
            owners = artifact_producers(self.profile)
            manifest = self.store.load()
            memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]] = {}
            records: dict[str, Mapping[str, Any]] = {}
            for name in sorted(set(owners.values())):
                valid, reason, record = self._cache_state(name, manifest, memo, set(), target)
                if not valid or record is None:
                    if required:
                        raise FlowError("FLOW-BUNDLE-STALE", f"bundle 阶段 {name!r} 无效：{reason}")
                    return None
                records[name] = record
            return refresh_bundle(self.profile, records)
        except FlowError:
            raise
        except (BundleError, FingerprintError, ManifestError, OSError, ValueError) as exc:
            raise FlowError("FLOW-BUNDLE-INVALID", str(exc)) from exc

    def _resource_keys(self, stage: StageConfig, target: str | None) -> set[str]:
        keys = {f"resource:{item}" for item in stage.resources}
        exclusive_toolchains = {"vivado", "vitis", "xsct", "program_flash", "bootgen"}
        for name in stage.toolchains:
            config = self.profile.toolchains[name]
            executable = config.command[0]
            resolved = shutil.which(executable) or executable
            normalized = os.path.normcase(str(Path(resolved).expanduser().resolve(strict=False)))
            if name.lower() in exclusive_toolchains or Path(resolved).name.lower() in exclusive_toolchains:
                keys.add(f"toolchain:{normalized}")
        runner = _runner_name(stage)
        if target and (stage.kind in {"deploy", "validate"} or runner.startswith("amd.")):
            profile = self.profile.targets.get(stage.target or target, {})
            explicit = profile.get("hardware_identity") if isinstance(profile, Mapping) else None
            if isinstance(explicit, Mapping) and explicit:
                identity = dict(explicit)
            elif isinstance(profile, Mapping):
                identity = {key: profile[key] for key in ("board_id", "serial_number", "jtag_id", "target_id")
                            if key in profile}
            else:
                identity = {}
            board_key = json.dumps(identity, sort_keys=True, ensure_ascii=False) if identity else (stage.target or target)
            keys.add(f"target:{board_key}")
        return keys

    def _execute_schedule(
        self,
        order: Sequence[str],
        execute_names: Sequence[str],
        *,
        target: str | None,
        force: bool,
        allow_side_effects: bool,
        deploy_barrier: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        ordered = list(order)
        deploy_barrier = deploy_barrier or set()
        execute_order = [name for name in ordered if name in set(execute_names)]
        execute_set = set(execute_order)
        for name in ordered:
            stage = self.profile.stages[name]
            if name not in execute_set:
                self._require_cached(name, stage.target or target)
                continue
            self._check_runner(stage)
            self._check_side_effects((name,), allow_side_effects)
            if stage.kind not in {"build", "test", "validate", "deploy"}:
                raise FlowError("FLOW-STAGE-KIND", f"不支持的阶段类型：{stage.kind}")

        completed = set(ordered) - execute_set
        pending = list(execute_order)
        running: dict[Any, tuple[str, set[str]]] = {}
        results: dict[str, dict[str, Any]] = {}
        failures: dict[str, Exception] = {}
        blocked: dict[str, str] = {}
        active_resources: set[str] = set()
        max_workers = max(1, self.profile.max_parallel_stages)

        order_set = set(ordered)

        def dependencies_ready(name: str) -> bool:
            stage = self.profile.stages[name]
            needs = all(dep in completed or dep not in order_set for dep in stage.needs)
            if not needs:
                return False
            if stage.kind == "deploy" and deploy_barrier and not deploy_barrier.issubset(completed):
                return False
            return True

        def failed_dependency(name: str) -> str | None:
            stage = self.profile.stages[name]
            dependencies = list(stage.needs)
            if stage.kind == "deploy":
                dependencies.extend(deploy_barrier)
            return next((dep for dep in dependencies if dep in failures or dep in blocked), None)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fpga-flow") as pool:
            while pending or running:
                made_progress = False
                for name in list(pending):
                    failed_dep = failed_dependency(name)
                    if failed_dep is not None:
                        blocked[name] = f"前置阶段 {failed_dep!r} 未通过"
                        pending.remove(name)
                        made_progress = True
                for name in list(pending):
                    if len(running) >= max_workers:
                        break
                    if not dependencies_ready(name):
                        continue
                    stage = self.profile.stages[name]
                    resources = self._resource_keys(stage, target)
                    if resources & active_resources:
                        continue
                    active_stage_names = {entry[0] for entry in running.values()}
                    output_conflict = any(
                        _output_patterns_overlap(left, right)
                        for active_name in active_stage_names
                        for left in stage.outputs
                        for right in self.profile.stages[active_name].outputs
                    )
                    if output_conflict:
                        continue
                    future = pool.submit(
                        self._execute_stage, stage, target=target, force=force,
                        allow_side_effects=allow_side_effects,
                    )
                    running[future] = (name, resources)
                    active_resources.update(resources)
                    pending.remove(name)
                    made_progress = True
                if running:
                    done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                    for future in done:
                        name, resources = running.pop(future)
                        active_resources.difference_update(resources)
                        try:
                            results[name] = future.result()
                            completed.add(name)
                        except Exception as exc:
                            failures[name] = exc
                    continue
                if pending and not made_progress:
                    for name in pending:
                        blocked[name] = "依赖阶段未完成，无法调度"
                    pending.clear()

        if len(failures) == 1 and not blocked:
            error = next(iter(failures.values()))
            if isinstance(error, FlowError):
                raise error
            raise FlowError("FLOW-STAGE-FAILED", str(error)) from error
        if failures or blocked:
            details = [f"{name}: {message}" for name, message in failures.items()]
            details.extend(f"{name}: {message}" for name, message in blocked.items())
            raise FlowError("FLOW-SCHEDULE-FAILED", "阶段调度未完成；" + "；".join(details))
        return [results[name] for name in execute_order]

    def build(self, stage: str, *, with_deps: bool = False, force: bool = False,
              target: str | None = None, allow_side_effects: bool = False,
              wait_lock: bool = False) -> dict[str, Any]:
        with self._mutation_guard("build", wait_lock=wait_lock):
            requested = self.profile.stages.get(stage)
            if requested is None:
                raise FlowError("FLOW-STAGE-UNKNOWN", f"未知阶段：{stage}")
            if requested.kind != "build":
                raise FlowError("FLOW-STAGE-KIND", f"build 需要 kind=build，但阶段 {stage!r} 的 kind 是 {requested.kind!r}")
            selected_target = self._target(target, requested)
            order = topological_order(self.profile, [stage]) if with_deps else [stage]
            if any(self.profile.stages[name].kind == "deploy" for name in order):
                raise FlowError("FLOW-RELEASE-DEPLOY-BLOCKED", "build --with-deps 不会部署；请显式调用 deploy")
            execute = order if with_deps else [stage]
            completed = self._execute_schedule(
                order, execute, target=selected_target, force=force,
                allow_side_effects=allow_side_effects,
            )
            self._refresh_bundle(selected_target, required=False)
            return {"status": "succeeded", "operation": "build", "project": self.profile.project_name,
                    "target": selected_target, "stages": completed}

    def validate(self, suite: str, *, force: bool = False, target: str | None = None,
                 allow_side_effects: bool = False, wait_lock: bool = False) -> dict[str, Any]:
        with self._mutation_guard("validate", wait_lock=wait_lock):
            if suite not in self.profile.suites:
                raise FlowError("FLOW-SUITE-UNKNOWN", f"未知验证套件 {suite!r}")
            selected_target = self._target(target)
            suite_stages = self.profile.suites[suite]
            order = topological_order(self.profile, suite_stages)
            selected = set(suite_stages)
            for name in suite_stages:
                stage = self.profile.stages[name]
                if stage.kind not in {"test", "validate"}:
                    raise FlowError("FLOW-SUITE-KIND", f"套件阶段 {name!r} 的 kind 必须是 test 或 validate")
                if selected_target and stage.target and stage.target != selected_target:
                    raise FlowError("FLOW-TARGET-MISMATCH", f"套件 {suite!r} 包含为目标 {stage.target!r} 配置的阶段 {name!r}")
            results = self._execute_schedule(
                order, selected, target=selected_target, force=force,
                allow_side_effects=allow_side_effects,
            )
            return {"status": "succeeded", "operation": "validate", "suite": suite,
                    "project": self.profile.project_name, "target": selected_target, "stages": results}

    def _deploy_stages(self, target: str) -> list[str]:
        return [name for name, stage in self.profile.stages.items()
                if stage.kind == "deploy" and stage.target == target]

    def _check_destructive(self, stage_names: Sequence[str], allow_destructive: bool) -> None:
        destructive = [name for name in stage_names if self.profile.stages[name].destructive]
        if destructive and not allow_destructive:
            raise FlowError("FLOW-DESTRUCTIVE-ACK", "破坏性阶段必须使用 --allow-destructive 显式确认：" + ", ".join(destructive))

    def _select_deploy_stages(self, target: str, stage_name: str | None = None) -> list[str]:
        candidates = self._deploy_stages(target)
        if stage_name is not None:
            stage = self.profile.stages.get(stage_name)
            if stage is None or stage.kind != "deploy":
                raise FlowError("FLOW-DEPLOY-STAGE-UNKNOWN", f"未知部署阶段：{stage_name!r}")
            if stage.target != target:
                raise FlowError("FLOW-DEPLOY-TARGET-MISMATCH", f"阶段 {stage_name!r} 未配置用于目标 {target!r}")
            return [stage_name]
        if not candidates:
            raise FlowError("FLOW-DEPLOY-STAGE-MISSING", f"目标 {target!r} 没有配置部署阶段")
        if len(candidates) > 1:
            raise FlowError(
                "FLOW-DEPLOY-AMBIGUOUS",
                f"目标 {target!r} 对应多个部署阶段：{', '.join(candidates)}；请显式指定 --stage",
            )
        return candidates

    def deploy(self, target: str, *, stage_name: str | None = None,
               allow_destructive: bool = False, allow_side_effects: bool = False,
               wait_lock: bool = False) -> dict[str, Any]:
        with self._mutation_guard("deploy", wait_lock=wait_lock):
            selected_target = self._target(target)
            if selected_target is None:
                raise FlowError("FLOW-TARGET-REQUIRED", "deploy 必须显式指定 --target")
            selected = self._select_deploy_stages(selected_target, stage_name)
            self._check_destructive(selected, allow_destructive)
            self._check_side_effects(selected, allow_side_effects)
            order = topological_order(self.profile, selected)
            results = self._execute_schedule(
                order, selected, target=selected_target, force=True,
                allow_side_effects=allow_side_effects,
            )
            return {"status": "succeeded", "operation": "deploy", "project": self.profile.project_name,
                    "target": selected_target, "deploy_stage": selected[0], "stages": results}

    def release(self, *, target: str | None = None, deploy: bool = False,
                deploy_stage: str | None = None, allow_destructive: bool = False,
                allow_side_effects: bool = False, force: bool = False,
                wait_lock: bool = False) -> dict[str, Any]:
        with self._mutation_guard("release", wait_lock=wait_lock):
            selected_target = self._target(target)
            if deploy_stage is not None and not deploy:
                raise FlowError("FLOW-RELEASE-DEPLOY-OPTIN", "--deploy-stage 必须与显式的 --deploy 一起使用")
            if deploy and selected_target is None:
                raise FlowError("FLOW-TARGET-REQUIRED", "release --deploy 必须指定 --target")
            matching_deploy = self._select_deploy_stages(selected_target, deploy_stage) if deploy and selected_target else []
            if deploy:
                self._check_destructive(matching_deploy, allow_destructive)
                for name in matching_deploy:
                    stage = self.profile.stages[name]
                    options = dict(self.profile.targets.get(stage.target or selected_target, {}))
                    options.update(stage.options)
                    if _runner_name(stage) == "amd.qspi.program" and options.get("verify") is not True:
                        raise FlowError("FLOW-RELEASE-QSPI-VERIFY", "发布 QSPI 必须启用读回校验")
            selected = [name for name, stage in self.profile.stages.items()
                        if stage.kind in {"build", "test", "validate"}
                        and (selected_target is None or stage.target is None or stage.target == selected_target)]
            if deploy:
                selected.extend(matching_deploy)
            self._check_side_effects(selected, allow_side_effects)
            order = topological_order(self.profile, selected)
            deployment_names = set(matching_deploy)

            def depends_on_deployment(name: str) -> bool:
                pending = list(self.profile.stages[name].needs)
                visited: set[str] = set()
                while pending:
                    dependency = pending.pop()
                    if dependency in deployment_names:
                        return True
                    if dependency not in visited:
                        visited.add(dependency)
                        pending.extend(self.profile.stages[dependency].needs)
                return False

            barrier = {
                name for name in selected
                if self.profile.stages[name].kind in {"build", "test", "validate"}
                and not depends_on_deployment(name)
            }
            results = self._execute_schedule(
                order, selected, target=selected_target, force=force,
                allow_side_effects=allow_side_effects, deploy_barrier=barrier if deploy else set(),
            )
            self._refresh_bundle(selected_target, required=False)
            return {"status": "succeeded", "operation": "release", "project": self.profile.project_name,
                    "target": selected_target, "deploy_requested": deploy,
                    "deploy_stage": matching_deploy[0] if matching_deploy else None, "stages": results}

    def _scope_stages(self, *, target: str | None = None, stage: str | None = None,
                      suite: str | None = None, include_deploy_by_default: bool = True
                      ) -> tuple[str | None, list[str], list[str]]:
        if stage is not None and suite is not None:
            raise FlowError("FLOW-SCOPE-CONFLICT", "stage 与 suite 不能同时指定")
        selected_target = self._target(target)
        if stage is not None:
            if stage not in self.profile.stages:
                raise FlowError("FLOW-STAGE-UNKNOWN", f"未知阶段：{stage}")
            requested = [stage]
        elif suite is not None:
            if suite not in self.profile.suites:
                raise FlowError("FLOW-SUITE-UNKNOWN", f"未知验证套件 {suite!r}")
            requested = list(self.profile.suites[suite])
        else:
            requested = [
                name for name, item in self.profile.stages.items()
                if include_deploy_by_default or item.kind != "deploy"
            ]
        for name in requested:
            item = self.profile.stages[name]
            if selected_target and item.target and item.target != selected_target:
                raise FlowError("FLOW-TARGET-MISMATCH",
                                f"阶段 {name!r} 固定使用目标 {item.target!r}")
        order = topological_order(self.profile, requested) if requested else []
        return selected_target, requested, order

    def _config_argument(self) -> str:
        try:
            return self.profile.config_path.relative_to(self.profile.root).as_posix()
        except ValueError:
            return str(self.profile.config_path)

    @staticmethod
    def _command_text(argv: Sequence[str]) -> str:
        return subprocess.list2cmdline(list(argv)) if os.name == "nt" else shlex.join(list(argv))

    def _cli_argv(self, action: str, *arguments: str) -> list[str]:
        return ["fpga-flow", "--config", self._config_argument(), action, *arguments]

    def _stage_action(self, stage: StageConfig, *, target: str | None,
                      suite: str | None = None, with_deps: bool = False) -> list[str] | None:
        if stage.kind == "build":
            args = ["--stage", stage.name]
            affected = topological_order(self.profile, [stage.name]) if with_deps else [stage.name]
            if with_deps:
                args.append("--with-deps")
            action = "build"
        elif stage.kind in {"test", "validate"}:
            matching = [name for name, names in self.profile.suites.items() if stage.name in names]
            selected_suite = suite if suite in matching else (matching[0] if matching else None)
            if selected_suite is None:
                return None
            args = ["--suite", selected_suite]
            affected = list(self.profile.suites[selected_suite])
            action = "validate"
        elif stage.kind == "deploy":
            selected = self._select_deploy_stages(target or stage.target or "", stage.name)
            args = ["--target", target or stage.target or "", "--stage", selected[0]]
            affected = [stage.name]
            if stage.destructive:
                args.append("--allow-destructive")
            action = "deploy"
        else:
            return None
        selected_target = target or stage.target
        if selected_target and "--target" not in args:
            args.extend(["--target", selected_target])
        if any(self.profile.stages[name].side_effects for name in affected):
            args.append("--allow-side-effects")
        return self._cli_argv(action, *args)

    def _file_tail(self, path: Path, count: int) -> list[str]:
        if count <= 0:
            return []
        try:
            from collections import deque
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                return [line.rstrip("\r\n") for line in deque(stream, maxlen=count)]
        except OSError:
            return []

    def doctor(self, *, target: str | None = None, stage: str | None = None,
               suite: str | None = None, wait_lock: bool = False) -> dict[str, Any]:
        selected_target, requested, _ = self._scope_stages(
            target=target, stage=stage, suite=suite, include_deploy_by_default=True
        )
        scope_names = requested if stage is not None or suite is not None else [
            name for name, item in self.profile.stages.items()
            if selected_target is None or item.target is None or item.target == selected_target
        ]
        toolchain_names = list(dict.fromkeys(
            toolchain for name in scope_names for toolchain in self.profile.stages[name].toolchains
        ))
        if not scope_names and stage is None and suite is None and selected_target is None:
            toolchain_names = list(self.profile.toolchains)

        with self._mutation_guard("doctor", wait_lock=wait_lock):
            run_id = _new_run_id()
            run_dir = self.store.run_path(run_id).parent
            run_dir.mkdir(parents=True, exist_ok=False)
            if self._active_run_lock is not None:
                self._active_run_lock.set_run(run_id)
            started = utc_now()
            self._notify({"event": "doctor_started", "run_id": run_id, "target": selected_target})
            versions, details, okay = self._probe_all_toolchains(run_dir, toolchain_names)
            preflight: dict[str, dict[str, Any]] = {}
            warnings: list[str] = []
            for name in scope_names:
                item = self.profile.stages[name]
                runner = _runner_name(item)
                if runner == "command":
                    continue
                provider = self.providers.get(runner)
                probe = getattr(provider, "preflight", None) if provider is not None else None
                if not callable(probe):
                    preflight[name] = {"status": "not_probed", "runner": runner,
                                       "detail": "provider 未提供只读预检"}
                    continue
                stage_target = item.target or selected_target
                try:
                    preflight_run_id = f"doctor-{run_id}"
                    with self._stage_target_lock(item, stage_target, preflight_run_id):
                        result = dict(probe(item, self.profile, run_dir / "preflight" / name, stage_target))
                    probe_status = str(result.get("status", "ok"))
                    preflight[name] = {"status": probe_status, "runner": runner, "result": result}
                    if probe_status == "warning":
                        warnings.append(f"{name}: {result.get('detail', '预检警告')}")
                    elif probe_status != "ok":
                        okay = False
                except Exception as exc:
                    preflight[name] = {"status": "failed", "runner": runner,
                                       "error": f"{type(exc).__name__}: {exc}"}
                    okay = False

            toolchain_warnings = [name for name, value in details.items()
                                  if value.get("status") == "warning"]
            warnings.extend(f"{name}: {details[name].get('detail')}" for name in toolchain_warnings)
            status = "failed" if not okay else ("warning" if warnings else "succeeded")
            completed = utc_now()
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION, "stage": "doctor", "status": status,
                "target": selected_target, "scope": {"stage": stage, "suite": suite},
                "selected_stages": scope_names, "tool_versions": versions,
                "toolchain_details": details, "preflight_details": preflight,
                "warnings": warnings, "inputs": {}, "dependencies": {}, "outputs": {},
                "command": [], "started_at_utc": started, "completed_at_utc": completed,
            }
            _write_json(run_dir / "command.json", {"action": "doctor", "target": selected_target,
                                                    "stage": stage, "suite": suite})
            _write_json(run_dir / "execution.json", {"succeeded": okay, "started_at_utc": started,
                                                       "completed_at_utc": completed})
            (run_dir / "stdout.log").write_text(json.dumps(
                {"toolchains": details, "preflight": preflight}, ensure_ascii=False, indent=2
            ) + "\n", encoding="utf-8")
            (run_dir / "stderr.log").write_text("\n".join(warnings) + ("\n" if warnings else ""),
                                                encoding="utf-8")
            record["evidence"] = _evidence_hashes(self.profile.root, run_dir)
            self.store.record_run(run_id, "doctor", record)
            self._notify({"event": "doctor_finished", "run_id": run_id, "status": status})
            return {"run_id": run_id, **record}

    def plan(self, *, target: str | None = None, stage: str | None = None,
             suite: str | None = None) -> dict[str, Any]:
        selected_target, requested, order = self._scope_stages(
            target=target, stage=stage, suite=suite, include_deploy_by_default=True
        )
        manifest = self.store.load()
        memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]] = {}
        rows: list[dict[str, Any]] = []
        for name in order:
            item = self.profile.stages[name]
            summary = manifest.get("stages", {}).get(name, {})
            last_elapsed = summary.get("elapsed_seconds") if isinstance(summary, Mapping) else None
            row: dict[str, Any] = {
                "stage": name, "kind": item.kind, "runner": _runner_name(item),
                "target": item.target, "needs": list(item.needs),
                "toolchains": list(item.toolchains), "resources": list(item.resources),
                "side_effects": list(item.side_effects),
                "requires_side_effect_ack": bool(item.side_effects),
                "description": item.description, "timeout_seconds": item.timeout_seconds,
                "last_run_id": summary.get("run_id") if isinstance(summary, Mapping) else None,
                "last_status": summary.get("status") if isinstance(summary, Mapping) else None,
                "last_success_run_id": (summary.get("last_success_run_id") or (summary.get("run_id") if summary.get("status") == "succeeded" else None)) if isinstance(summary, Mapping) else None,
                "last_elapsed_seconds": last_elapsed,
            }
            try:
                self._check_runner(item)
            except FlowError as exc:
                row.update({"action": "blocked", "status": "fail",
                            "detail": f"{exc.code}: {exc}"})
                rows.append(row)
                continue
            if item.kind == "deploy":
                if item.target and selected_target and item.target != selected_target:
                    row.update({"action": "skip-target", "status": "not-selected",
                                "detail": f"阶段固定使用目标 {item.target!r}"})
                elif stage == name:
                    row.update({"action": "deploy", "status": "explicit-only",
                                "detail": "需显式选择该部署阶段"})
                else:
                    row.update({"action": "explicit-deploy-only", "status": "blocked",
                                "detail": "部署阶段不会由 plan 或 release 自动执行"})
                rows.append(row)
                continue
            if item.kind == "validate":
                row.update({"action": "validate", "status": "explicit-only",
                            "detail": "验证阶段每次显式请求都会重新运行"})
                rows.append(row)
                continue
            try:
                valid, detail, record = self._cache_state(name, manifest, memo, set(), selected_target)
                evidence = _evidence_status(self.profile.root, record, self.store) if record else "missing"
                command = list(item.command)
                if _runner_name(item) == "command" and item.command:
                    fields = {"root": str(self.profile.root), "out_dir": str(self.profile.output_dir),
                              "run_dir": "<run_dir>", "target": selected_target or ""}
                    command = list(_format_argv(item.command, fields))
                if valid:
                    row.update({"action": "cache-hit", "status": "succeeded", "detail": detail,
                                "run_id": record.get("run_id") if record else None, "evidence": evidence})
                else:
                    row.update({"action": "run", "status": "pending", "detail": detail,
                                "evidence": evidence, "command_argv": command})
            except FlowError as exc:
                row.update({"action": "blocked", "status": "fail", "detail": f"{exc.code}: {exc}"})
            rows.append(row)
        return {
            "status": "ready", "project": self.profile.project_name, "target": selected_target,
            "scope": {"stage": stage, "suite": suite}, "commands_launched": False,
            "max_parallel_stages": self.profile.max_parallel_stages, "stages": rows,
        }

    def status(self, *, target: str | None = None, stage: str | None = None,
               suite: str | None = None) -> dict[str, Any]:
        manifest = self.store.load()
        plan = self.plan(target=target, stage=stage, suite=suite)
        stage_records = manifest.get("stages", {})
        rows: list[dict[str, Any]] = []
        for row in plan["stages"]:
            summary = stage_records.get(row["stage"], {}) if isinstance(stage_records, Mapping) else {}
            try:
                previous = self.store.stage_record(manifest, row["stage"]) or summary
            except ManifestError:
                previous = summary
            last_status = summary.get("status") if isinstance(summary, Mapping) else None
            status_row = {
                **row,
                "last_status": last_status,
                "last_run_id": summary.get("run_id") if isinstance(summary, Mapping) else None,
                "last_error": previous.get("error") if isinstance(previous, Mapping) else None,
                "last_success_run_id": (summary.get("last_success_run_id") or (summary.get("run_id") if last_status == "succeeded" else None)) if isinstance(summary, Mapping) else None,
                "last_evidence": _evidence_status(self.profile.root, previous, self.store)
                if isinstance(previous, Mapping) else "missing",
            }
            if (row.get("action") in {"explicit-deploy-only", "validate"}
                    and last_status in {"running", "succeeded", "failed"}):
                status_row["status"] = last_status
            rows.append(status_row)
        run_ids = list(manifest.get("run_ids", []))
        return {
            "status": "ok", "project": self.profile.project_name, "target": plan["target"],
            "latest_run_id": manifest.get("latest_run_id"), "run_count": manifest.get("run_count", len(run_ids)),
            "recent_run_ids": run_ids[-10:], "stages": rows,
        }

    def resume(self, *, target: str | None = None, stage: str | None = None,
               suite: str | None = None) -> dict[str, Any]:
        selected_target, requested, order = self._scope_stages(
            target=target, stage=stage, suite=suite, include_deploy_by_default=False
        )
        manifest = self.store.load()
        plan = self.plan(target=selected_target, stage=stage, suite=suite)
        row_by_name = {row["stage"]: row for row in plan["stages"]}
        stage_records = manifest.get("stages", {})
        if stage is not None or suite is not None:
            failure_order = order
        else:
            failure_order = [
                name for name, item in self.profile.stages.items()
                if selected_target is None or item.target is None or item.target == selected_target
            ]
        for name in failure_order:
            summary = stage_records.get(name, {}) if isinstance(stage_records, Mapping) else {}
            if not isinstance(summary, Mapping) or summary.get("status") not in {"running", "failed"}:
                continue
            run_id = summary.get("run_id")
            command = self._cli_argv("watch", "--run-id", str(run_id), "--tail-lines", "80")
            return {
                "status": "needs-attention", "project": self.profile.project_name,
                "target": selected_target, "latest_run_id": manifest.get("latest_run_id"),
                "next_action": {"kind": "inspect-run", "stage": name, "run_id": run_id,
                                "reason": summary.get("error") or "阶段运行尚未完成",
                                "argv": command, "command": self._command_text(command)},
            }
        plan_rows = row_by_name
        if stage is None and suite is None:
            requested = [name for name in order if self.profile.stages[name].kind != "deploy"]
        for name in order:
            if name not in plan_rows:
                continue
            row = plan_rows[name]
            item = self.profile.stages[name]
            action = row.get("action")
            if action == "cache-hit" or action == "skip-target":
                continue
            if action == "blocked" and item.kind != "deploy":
                return {
                    "status": "needs-attention", "project": self.profile.project_name,
                    "target": selected_target, "latest_run_id": manifest.get("latest_run_id"),
                    "next_action": {"kind": "blocked", "stage": name, "reason": row.get("detail")},
                }
            if item.kind == "deploy":
                if stage != name:
                    required = any(name in topological_order(self.profile, [requested_name])[1:]
                                   for requested_name in requested)
                    if not required:
                        continue
                if stage != name:
                    valid_deploy, _, _ = self._cache_state(
                        name, manifest=manifest, target=item.target or selected_target
                    )
                    if valid_deploy:
                        continue
                argv = self._stage_action(item, target=selected_target)
            elif item.kind in {"test", "validate"}:
                matching_suites = [key for key, names in self.profile.suites.items() if name in names]
                chosen_suite = suite if suite in matching_suites else (matching_suites[0] if matching_suites else None)
                if chosen_suite is None:
                    return {
                        "status": "needs-attention", "project": self.profile.project_name,
                        "target": selected_target, "latest_run_id": manifest.get("latest_run_id"),
                        "next_action": {"kind": "blocked", "stage": name,
                                        "reason": "阶段未加入任何验证套件"},
                    }
                argv = self._stage_action(item, target=selected_target, suite=chosen_suite)
            else:
                dependency_stale = any(
                    not self._cache_state(dep, manifest=manifest, target=self.profile.stages[dep].target or selected_target)[0]
                    for dep in item.needs if self.profile.stages[dep].kind != "deploy"
                )
                argv = self._stage_action(item, target=selected_target, with_deps=dependency_stale)
            if argv is None:
                continue
            return {
                "status": "pending", "project": self.profile.project_name,
                "target": selected_target, "latest_run_id": manifest.get("latest_run_id"),
                "next_action": {"kind": action, "stage": name, "reason": row.get("detail"),
                                "side_effects": list(item.side_effects),
                                "argv": argv, "command": self._command_text(argv)},
            }
        return {
            "status": "ready", "project": self.profile.project_name,
            "target": selected_target, "latest_run_id": manifest.get("latest_run_id"),
            "next_action": None, "detail": "所选范围没有待执行阶段",
        }

    def watch(self, run_id: str, *, tail_lines: int = 40) -> dict[str, Any]:
        if type(tail_lines) is not int or not 0 <= tail_lines <= 500:
            raise FlowError("FLOW-WATCH-TAIL", "tail_lines 必须是 0 到 500 之间的整数")
        try:
            record_path = self.store.run_path(run_id)
        except ManifestError as exc:
            raise FlowError("FLOW-RUN-ID", str(exc)) from exc
        run_dir = record_path.parent
        if not run_dir.is_dir():
            raise FlowError("FLOW-RUN-NOT-FOUND", f"没有找到 run_id={run_id}")
        record = None
        if record_path.is_file():
            try:
                record = self.store.read_run_record(run_id)
            except ManifestError:
                record = None
        execution: dict[str, Any] = {}
        execution_path = run_dir / "execution.json"
        if execution_path.is_file():
            try:
                value = json.loads(execution_path.read_text(encoding="utf-8"))
                execution = value if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                execution = {}
        summary = None
        manifest = self.store.load()
        for value in manifest.get("stages", {}).values():
            if isinstance(value, Mapping) and value.get("run_id") == run_id:
                summary = value
                break
        status = record.get("status") if isinstance(record, Mapping) else None
        status = status or execution.get("state") or (summary.get("status") if isinstance(summary, Mapping) else "unknown")
        log_files = []
        for path in sorted(run_dir.rglob("*.log"), key=lambda p: p.stat().st_mtime):
            relative = path.relative_to(run_dir).as_posix()
            log_files.append({"path": relative, "tail": self._file_tail(path, tail_lines)})
        return {
            "status": status, "run_id": run_id,
            "stage": (record or summary or {}).get("stage"),
            "target": (record or summary or {}).get("target"),
            "started_at_utc": (record or {}).get("started_at_utc") or execution.get("started_at_utc"),
            "completed_at_utc": (record or {}).get("completed_at_utc") if isinstance(record, Mapping) else None,
            "error_code": (record or {}).get("error_code") if isinstance(record, Mapping) else None,
            "error": (record or {}).get("error") if isinstance(record, Mapping) else None,
            "execution": execution, "logs": log_files,
        }

    def history(self, *, limit: int = 20, before_run_id: str | None = None,
                stage: str | None = None) -> dict[str, Any]:
        try:
            records = self.store.list_runs(limit=limit, before_run_id=before_run_id, stage=stage)
        except ManifestError as exc:
            raise FlowError("FLOW-HISTORY", str(exc)) from exc
        summaries = [{
            "run_id": item.get("run_id"), "stage": item.get("stage"), "kind": item.get("kind"),
            "status": item.get("status"), "target": item.get("target"),
            "started_at_utc": item.get("started_at_utc"),
            "completed_at_utc": item.get("completed_at_utc"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "error_code": item.get("error_code"), "error": item.get("error"),
        } for item in records]
        return {"status": "ok", "project": self.profile.project_name,
                "count": len(summaries), "stage_filter": stage, "runs": summaries}
