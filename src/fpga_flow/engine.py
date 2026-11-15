"""面向不同 AMD FPGA 项目的构建、验证与板级部署编排引擎。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Mapping, Protocol, Sequence
import json
import uuid

from .config import ProfileError, topological_order
from .fingerprint import (
    FingerprintError,
    expand_inputs,
    hash_artifacts,
    hash_inputs,
    sha256_file,
    stage_fingerprint,
)
from .manifest import ManifestError, ManifestStore, SCHEMA_VERSION, utc_now
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


class FlowEngine:
    """按项目定义的阶段图执行任务，并记录缓存与运行来源。"""
    def __init__(self, profile: FlowConfig, *, providers: Mapping[str, StageProvider] | None = None):
        self.profile = profile
        self.store = ManifestStore(profile.root, profile.output_dir, profile.project_name)
        self.providers = dict(_PROVIDER_REGISTRY)
        if providers:
            self.providers.update(providers)
        self._completed_in_session: set[str] = set()

    @classmethod
    def from_file(cls, config_path: str | Path = "fpga-flow.toml", *, providers: Mapping[str, StageProvider] | None = None) -> "FlowEngine":
        from .config import load_profile
        return cls(load_profile(config_path), providers=providers)

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
                                    cwd=self.profile.root, log_dir=run_dir / "toolchains" / name)
            detail = {"name": probe.name, "configured_command": probe.configured_command,
                      "resolved_command": probe.resolved_command, "expected_version": probe.expected_version,
                      "reported_version": probe.reported_version, "status": probe.status,
                      "detail": probe.detail, "unlocked": config.expected_version is None}
            details[name] = detail
            if not probe.ok:
                raise FlowError("FLOW-TOOLCHAIN", f"工具链 {name!r}：{probe.detail}")
            versions[name] = _tool_version_value(probe)
        return versions, details

    def _probe_all_toolchains(self, run_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]], bool]:
        versions: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        okay = True
        for name, config in self.profile.toolchains.items():
            command, version_args = self._toolchain_command(name)
            probe = probe_toolchain(name, command, version_args, config.expected_version,
                                    cwd=self.profile.root, log_dir=run_dir / "toolchains" / name)
            details[name] = {"name": probe.name, "configured_command": probe.configured_command,
                             "resolved_command": probe.resolved_command, "expected_version": probe.expected_version,
                             "reported_version": probe.reported_version, "status": probe.status,
                             "detail": probe.detail, "unlocked": config.expected_version is None}
            if probe.ok:
                versions[name] = _tool_version_value(probe)
            else:
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
        records = manifest.get("stages", {})
        record = records.get(stage_name) if isinstance(records, Mapping) else None
        context_target = stage.target or target or (record.get("target") if isinstance(record, Mapping) else None) or self.profile.default_target
        memo_key = (stage_name, context_target)
        if memo_key in memo:
            return memo[memo_key]
        if stage.kind == "validate" and stage_name not in self._completed_in_session:
            result = (False, "运行时/板级验证是证据，不可作为可复用缓存命中", record if isinstance(record, Mapping) else None)
            memo[memo_key] = result
            return result
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            result = (False, "没有成功的阶段运行记录", record if isinstance(record, Mapping) else None)
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
            fingerprint = stage_fingerprint(_stage_contract(self.profile, stage, context_target),
                                            inputs, dependency_hashes, versions)
            if record.get("fingerprint") != fingerprint:
                result = (False, "输入、依赖、工具链、目标或阶段配置已改变", record)
            elif stage.outputs and not _outputs_match(record, fingerprint, stage.outputs, self.profile.root):
                result = (False, "一个或多个必需产物缺失或哈希无效", record)
            elif not stage.outputs and record.get("outputs", {}) != {}:
                result = (False, "无输出阶段却记录了产物", record)
            else:
                result = (True, "输入指纹与产物哈希均匹配", record)
        except (FlowError, FingerprintError, OSError, ValueError) as exc:
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

    def _dispatch(self, stage: StageConfig, run_dir: Path, target: str | None) -> tuple[CommandResult, tuple[str, ...]]:
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
            result = run_command(command, cwd=cwd, env=env,
                                 timeout_seconds=stage.timeout_seconds, log_dir=run_dir)
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
        if status == "succeeded":
            self._completed_in_session.add(stage.name)
        return record

    def _execute_stage(self, stage: StageConfig, *, target: str | None = None, force: bool = False) -> dict[str, Any]:
        self._check_runner(stage)
        selected_target = self._target(target, stage)
        if stage.kind == "deploy" and target is None:
            raise FlowError("FLOW-TARGET-REQUIRED", f"部署阶段 {stage.name!r} 必须显式指定 --target")
        self._require_dependencies(stage, selected_target)
        # 发布 running 状态前先保留上一次完成的阶段记录，避免 running 指针覆盖可复用的缓存记录。
        pre_manifest = self.store.load()
        previous_stage_record = pre_manifest.get("stages", {}).get(stage.name, {})
        run_id = _new_run_id()
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
        try:
            inputs = hash_inputs(self.profile.root, stage.inputs, require_matches=bool(stage.inputs))
            versions, details = self._probe_stage_toolchains(stage, run_dir)
            manifest = self.store.load()
            dependencies, dependency_error = self._dependency_hashes(stage, manifest, {}, set(), selected_target)
            if dependencies is None:
                raise FlowError("FLOW-DEPENDENCY-STALE", dependency_error or "依赖阶段已过期")
            fingerprint = stage_fingerprint(
                _stage_contract(self.profile, stage, selected_target), inputs, dependencies, versions
            )
            previous = previous_stage_record
            cacheable = stage.kind in {"build", "test"} and not force
            if cacheable and isinstance(previous, Mapping) and _outputs_match(
                previous, fingerprint, stage.outputs, self.profile.root
            ):
                outputs = dict(previous.get("outputs", {}))
                (run_dir / "stdout.log").write_text("", encoding="utf-8")
                (run_dir / "stderr.log").write_text("", encoding="utf-8")
                _write_json(run_dir / "command.json", {"cache_hit": True, "stage": stage.name,
                                                        "target": selected_target, "argv": list(command_preview)})
                _write_json(run_dir / "execution.json", {"succeeded": True, "cache_hit": True})
                return self._complete_run(stage, run_id, "succeeded", started, target=selected_target,
                                          inputs=inputs, dependencies=dependencies, tool_versions=versions,
                                          toolchain_details=details, fingerprint=fingerprint, outputs=outputs,
                                          command=command_preview, cache_hit=True)

            command_result, command_preview = self._dispatch(stage, run_dir, selected_target)
            output_text = command_result.stdout + "\n" + command_result.stderr
            if not command_result.succeeded:
                raise FlowError("FLOW-COMMAND-FAILED", command_result.error or f"命令退出码为 {command_result.exit_code}")
            failed = [marker for marker in stage.failure_markers if marker in output_text]
            if failed:
                raise FlowError("FLOW-FAILURE-MARKER", f"检测到失败标记：{failed}")
            missing = [marker for marker in stage.success_markers if marker not in output_text]
            if missing:
                raise FlowError("FLOW-SUCCESS-MARKER-MISSING", f"缺少成功标记：{missing}")
            try:
                concrete_outputs, _ = _expand_output_patterns(self.profile.root, stage.outputs)
                outputs = hash_artifacts(self.profile.root, concrete_outputs)
            except (FlowError, FingerprintError, OSError, ValueError) as exc:
                raise FlowError("FLOW-OUTPUT-INVALID", str(exc)) from exc
            return self._complete_run(stage, run_id, "succeeded", started, target=selected_target,
                                      inputs=inputs, dependencies=dependencies, tool_versions=versions,
                                      toolchain_details=details, fingerprint=fingerprint, outputs=outputs,
                                      command=command_preview, elapsed_seconds=command_result.elapsed_seconds)
        except Exception as exc:
            error = exc if isinstance(exc, FlowError) else FlowError("FLOW-STAGE-FAILED", f"{type(exc).__name__}：{exc}")
            if not (run_dir / "stderr.log").is_file() or not (run_dir / "command.json").is_file():
                _write_failure_logs(run_dir, command_preview, f"{error.code}: {error}", started)
            (run_dir / "flow-error.txt").write_text(f"{error.code}: {error}\n", encoding="utf-8")
            self._complete_run(stage, run_id, "failed", started, target=selected_target,
                               inputs=inputs, dependencies=dependencies, tool_versions=versions,
                               toolchain_details=details, fingerprint=fingerprint, outputs={},
                               command=command_preview, error=error,
                               elapsed_seconds=command_result.elapsed_seconds if command_result else None)
            raise error from exc
    def _require_cached(self, stage_name: str, target: str | None = None) -> Mapping[str, Any]:
        valid, reason, record = self._cache_state(stage_name, target=target)
        if not valid or record is None:
            raise FlowError("FLOW-DEPENDENCY-STALE", f"必需阶段 {stage_name!r} 已过期：{reason}")
        return record

    def build(self, stage: str, *, with_deps: bool = False, force: bool = False,
              target: str | None = None) -> dict[str, Any]:
        requested = self.profile.stages.get(stage)
        if requested is None:
            raise FlowError("FLOW-STAGE-UNKNOWN", f"未知阶段：{stage}")
        if requested.kind != "build":
            raise FlowError("FLOW-STAGE-KIND", f"build 需要 kind=build，但阶段 {stage!r} 的 kind 是 {requested.kind!r}")
        selected_target = self._target(target, requested)
        order = topological_order(self.profile, [stage]) if with_deps else [stage]
        completed: list[dict[str, Any]] = []
        for name in order:
            current = self.profile.stages[name]
            if current.kind == "deploy":
                raise FlowError("FLOW-RELEASE-DEPLOY-BLOCKED", "build --with-deps 不会部署；请显式调用 deploy")
            if current.kind not in {"build", "test", "validate"}:
                raise FlowError("FLOW-STAGE-KIND", f"前置阶段 {name!r} 的 kind {current.kind!r} 不受支持")
            completed.append(self._execute_stage(current, target=selected_target, force=force))
        return {"status": "succeeded", "operation": "build", "project": self.profile.project_name,
                "target": selected_target, "stages": completed}

    def validate(self, suite: str, *, force: bool = False, target: str | None = None) -> dict[str, Any]:
        if suite not in self.profile.suites:
            raise FlowError("FLOW-SUITE-UNKNOWN", f"未知验证套件 {suite!r}")
        selected_target = self._target(target)
        suite_stages = self.profile.suites[suite]
        order = topological_order(self.profile, suite_stages)
        selected = set(suite_stages)
        results: list[dict[str, Any]] = []
        for name in order:
            stage = self.profile.stages[name]
            if selected_target and stage.target and stage.target != selected_target:
                if name in selected:
                    raise FlowError("FLOW-TARGET-MISMATCH", f"套件 {suite!r} 包含为目标 {stage.target!r} 配置的阶段 {name!r}")
                self._require_cached(name, stage.target)
                continue
            if stage.kind == "deploy":
                self._require_cached(name, selected_target or stage.target)
                continue
            if name not in selected:
                self._require_cached(name, selected_target)
                continue
            if stage.kind not in {"test", "validate"}:
                raise FlowError("FLOW-SUITE-KIND", f"套件阶段 {name!r} 的 kind 必须是 test 或 validate")
            results.append(self._execute_stage(stage, target=selected_target, force=force))
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
               allow_destructive: bool = False) -> dict[str, Any]:
        selected_target = self._target(target)
        if selected_target is None:
            raise FlowError("FLOW-TARGET-REQUIRED", "deploy 必须显式指定 --target")
        selected = self._select_deploy_stages(selected_target, stage_name)
        self._check_destructive(selected, allow_destructive)
        order = topological_order(self.profile, selected)
        results: list[dict[str, Any]] = []
        selected_set = set(selected)
        for name in order:
            stage = self.profile.stages[name]
            if stage.kind == "deploy":
                if name in selected_set and stage.target == selected_target:
                    results.append(self._execute_stage(stage, target=selected_target, force=True))
                else:
                    # 不会隐式串联不同的部署路径。
                    self._require_cached(name, stage.target or selected_target)
            else:
                self._require_cached(name, selected_target)
        return {"status": "succeeded", "operation": "deploy", "project": self.profile.project_name,
                "target": selected_target, "deploy_stage": selected[0], "stages": results}

    def release(self, *, target: str | None = None, deploy: bool = False,
                deploy_stage: str | None = None, allow_destructive: bool = False,
                force: bool = False) -> dict[str, Any]:
        selected_target = self._target(target)
        if deploy_stage is not None and not deploy:
            raise FlowError("FLOW-RELEASE-DEPLOY-OPTIN", "--deploy-stage 必须与显式的 --deploy 一起使用")
        if deploy and selected_target is None:
            raise FlowError("FLOW-TARGET-REQUIRED", "release --deploy 必须指定 --target")
        matching_deploy = self._select_deploy_stages(selected_target, deploy_stage) if deploy and selected_target else []
        if deploy:
            self._check_destructive(matching_deploy, allow_destructive)
        selected = [name for name, stage in self.profile.stages.items()
                    if stage.kind in {"build", "test", "validate"}
                    and (selected_target is None or stage.target is None or stage.target == selected_target)]
        if deploy:
            selected.extend(matching_deploy)
        order = topological_order(self.profile, selected)
        results: list[dict[str, Any]] = []
        selected_set = set(selected)
        for name in order:
            stage = self.profile.stages[name]
            if stage.kind == "deploy":
                if deploy and name in selected_set and stage.target == selected_target:
                    results.append(self._execute_stage(stage, target=selected_target, force=True))
                else:
                    # 依赖可以使用先前部署记录，但 release 永远不会自动运行未选定的部署阶段。
                    self._require_cached(name, stage.target or selected_target)
                continue
            if selected_target and stage.target and stage.target != selected_target:
                self._require_cached(name, stage.target)
                continue
            if name not in selected_set:
                self._require_cached(name, selected_target)
                continue
            if stage.kind not in {"build", "test", "validate"}:
                raise FlowError("FLOW-STAGE-KIND", f"release 阶段 kind {stage.kind!r} 不受支持")
            results.append(self._execute_stage(stage, target=selected_target, force=force))
        return {"status": "succeeded", "operation": "release", "project": self.profile.project_name,
                "target": selected_target, "deploy_requested": deploy,
                "deploy_stage": matching_deploy[0] if matching_deploy else None, "stages": results}
    def doctor(self, *, target: str | None = None) -> dict[str, Any]:
        selected_target = self._target(target)
        run_id = _new_run_id()
        run_dir = self.store.run_path(run_id).parent
        run_dir.mkdir(parents=True, exist_ok=False)
        started = utc_now()
        versions, details, okay = self._probe_all_toolchains(run_dir)
        completed = utc_now()
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "stage": "doctor",
            "status": "succeeded" if okay else "failed",
            "target": selected_target,
            "tool_versions": versions,
            "toolchain_details": details,
            "inputs": {},
            "dependencies": {},
            "outputs": {},
            "command": [],
            "started_at_utc": started,
            "completed_at_utc": completed,
        }
        _write_json(run_dir / "command.json", {"action": "doctor", "target": selected_target})
        _write_json(run_dir / "execution.json", {"succeeded": okay, "started_at_utc": started,
                                                   "completed_at_utc": completed})
        (run_dir / "stdout.log").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (run_dir / "stderr.log").write_text("" if okay else "一个或多个工具链探测未通过。\n", encoding="utf-8")
        record["evidence"] = _evidence_hashes(self.profile.root, run_dir)
        self.store.record_run(run_id, "doctor", record)
        return {"run_id": run_id, **record}

    def plan(self, *, target: str | None = None) -> dict[str, Any]:
        selected_target = self._target(target)
        manifest = self.store.load()
        memo: dict[tuple[str, str | None], tuple[bool, str, Mapping[str, Any] | None]] = {}
        rows: list[dict[str, Any]] = []
        for name, stage in self.profile.stages.items():
            row: dict[str, Any] = {"stage": name, "kind": stage.kind, "runner": _runner_name(stage),
                                   "target": stage.target, "needs": list(stage.needs)}
            try:
                self._check_runner(stage)
            except FlowError as exc:
                row.update({"action": "blocked", "status": "fail", "detail": f"{exc.code}: {exc}"})
                rows.append(row)
                continue
            if stage.kind == "deploy":
                if selected_target is None:
                    row.update({"action": "explicit-deploy-only", "status": "blocked",
                                "detail": "需要 deploy --target；plan 不会执行部署"})
                elif stage.target != selected_target:
                    row.update({"action": "skip-target", "status": "not-selected",
                                "detail": f"阶段目标为 {stage.target!r}"})
                else:
                    row.update({"action": "deploy", "status": "explicit-only",
                                "detail": "请显式部署此目标；部署阶段不会被缓存跳过"})
                rows.append(row)
                continue
            if stage.target and selected_target and stage.target != selected_target:
                row.update({"action": "skip-target", "status": "not-selected",
                            "detail": f"阶段固定使用目标 {stage.target!r}"})
                rows.append(row)
                continue
            if stage.kind == "validate":
                row.update({"action": "validate", "status": "explicit-only",
                            "detail": "板级/运行时验证在每次显式请求时都会重新执行"})
                rows.append(row)
                continue
            try:
                valid, detail, record = self._cache_state(name, manifest, memo, set(), selected_target)
                evidence = _evidence_status(self.profile.root, record, self.store) if record else "missing"
                if valid:
                    row.update({"action": "cache-hit", "status": "succeeded", "detail": detail,
                                "run_id": record.get("run_id") if record else None, "evidence": evidence})
                else:
                    row.update({"action": "run", "status": "pending", "detail": detail, "evidence": evidence})
            except FlowError as exc:
                row.update({"action": "blocked", "status": "fail", "detail": f"{exc.code}: {exc}"})
            rows.append(row)
        return {"status": "ready", "project": self.profile.project_name, "target": selected_target,
                "commands_launched": False, "stages": rows}

    def status(self, *, target: str | None = None) -> dict[str, Any]:
        manifest = self.store.load()
        plan = self.plan(target=target)
        stage_records = manifest.get("stages", {})
        rows: list[dict[str, Any]] = []
        for row in plan["stages"]:
            previous = stage_records.get(row["stage"], {}) if isinstance(stage_records, Mapping) else {}
            rows.append({**row,
                         "last_status": previous.get("status") if isinstance(previous, Mapping) else None,
                         "last_run_id": previous.get("run_id") if isinstance(previous, Mapping) else None,
                         "last_error": previous.get("error") if isinstance(previous, Mapping) else None,
                         "last_evidence": _evidence_status(self.profile.root, previous, self.store)
                         if isinstance(previous, Mapping) else "missing"})
        return {"status": "ok", "project": self.profile.project_name, "target": plan["target"],
                "latest_run_id": manifest.get("latest_run_id"), "run_ids": list(manifest.get("run_ids", [])),
                "stages": rows}

    def resume(self) -> dict[str, Any]:
        manifest = self.store.load()
        stage_records = manifest.get("stages", {})
        if not isinstance(stage_records, Mapping):
            raise FlowError("FLOW-MANIFEST", "manifest 的 stages 表格式无效")
        unfinished: list[dict[str, Any]] = []
        for name, record in stage_records.items():
            if not isinstance(record, Mapping) or record.get("status") not in {"running", "failed"}:
                continue
            run_id = record.get("run_id")
            log_dir = None
            if isinstance(run_id, str):
                try:
                    log_dir = str(self.store.run_path(run_id).parent)
                except ManifestError:
                    pass
            stage = self.profile.stages.get(name)
            suggestion = None
            if stage is not None:
                if stage.kind == "deploy":
                    suggestion = f"fpga-flow deploy --target {stage.target or '<target>'}"
                elif stage.kind == "build":
                    suggestion = f"fpga-flow build --stage {name}"
                else:
                    suite = next((suite_name for suite_name, names in self.profile.suites.items() if name in names), None)
                    if suite:
                        suggestion = f"fpga-flow validate --suite {suite}"
            unfinished.append({"stage": name, "status": record.get("status"), "run_id": run_id,
                               "error": record.get("error"), "log_dir": log_dir,
                               "evidence": _evidence_status(self.profile.root, record, self.store),
                               "suggested_command": suggestion})
        return {"status": "needs-attention" if unfinished else "ready",
                "project": self.profile.project_name, "latest_run_id": manifest.get("latest_run_id"),
                "unfinished_or_failed": unfinished,
                "next": "检查运行日志后重新查看计划" if unfinished else "运行 plan 查看当前缓存状态"}
