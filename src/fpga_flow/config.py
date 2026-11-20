"""加载并校验项目中的 fpga-flow.toml 配置。"""
from __future__ import annotations

import glob
import re
import tomllib
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .models import BundleConfig, ExternalDependencyConfig, FlowConfig, StageConfig, ToolchainConfig
from .bundle import BundleError, artifact_producers


SUPPORTED_SCHEMA_VERSION = 1
STAGE_KINDS = frozenset({"build", "test", "validate", "deploy"})
STAGE_RUNNERS = frozenset({"command", "amd.zynq.jtag", "amd.qspi.program", "amd.uart.commands"})
DEFAULT_OUTPUT_DIR = "out/fpga-flow"
_TOP_KEYS = {"schema_version", "project", "flow", "toolchains", "targets", "stages", "suites", "bundle", "external_dependencies"}
_PROJECT_KEYS = {"name", "description"}
_FLOW_KEYS = {"output_dir", "default_target", "max_parallel_stages"}
_TOOLCHAIN_KEYS = {"command", "version_args", "expected_version", "version_command", "failure_markers"}
_STAGE_KEYS = {
    "kind", "needs", "inputs", "outputs", "command", "cwd", "toolchains",
    "env", "timeout_seconds", "success_markers", "failure_markers",
    "side_effects", "resources", "target", "destructive", "description", "runner", "options", "external_inputs",
}


class ProfileError(ValueError):
    """配置文件是有效的 TOML，但不符合 Flow 配置约定。"""


def _table(value: object, where: str, *, allowed: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProfileError(f"{where} 必须是 TOML 表")
    if allowed is not None:
        extras = sorted(set(value) - allowed)
        if extras:
            raise ProfileError(f"{where} 包含不支持的键：{', '.join(extras)}")
    return value


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"{where} 必须是非空字符串")
    return value.strip()


def _strings(value: object, where: str, *, default: Sequence[str] = ()) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProfileError(f"{where} 必须是字符串数组")
    if any(not item.strip() for item in value):
        raise ProfileError(f"{where} 不能包含空字符串")
    if len(set(value)) != len(value):
        raise ProfileError(f"{where} 不能包含重复项")
    return tuple(value)


def _path_is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


def _reject_symlink_components(root: Path, parts: Sequence[str], where: str) -> None:
    """在规范化配置路径前拒绝已存在的符号链接或目录联接。"""
    current = root.resolve()
    for part in parts:
        if any(ch in part for ch in "*?[]"):
            # 通配符匹配项会在展开输入模式时再次检查。
            break
        current = current / part
        if _path_is_link(current):
            raise ProfileError(f"{where} 遍历了符号链接或目录联接：{current}")


def _reject_symlink_glob_matches(root: Path, pattern: Path, where: str) -> None:
    """规范化 glob 配置路径前，拒绝已存在的符号链接匹配项。"""
    checked_components: set[Path] = {root}
    for match in glob.iglob(str(pattern), recursive=True):
        lexical = Path(match)
        try:
            relative = lexical.relative_to(root)
        except ValueError as exc:
            raise ProfileError(f"{where} 的 glob 匹配到了项目根目录之外的路径：{match}") from exc
        current = root
        for part in relative.parts:
            current = current / part
            if current not in checked_components:
                if _path_is_link(current):
                    raise ProfileError(f"{where} 遍历了符号链接或目录联接：{current}")
                checked_components.add(current)


def _relative_path(
    value: object,
    where: str,
    root: Path,
    *,
    allow_glob: bool,
    allow_dot: bool = False,
) -> Path:
    raw = _string(value, where)
    if chr(92) in raw or chr(0) in raw or raw.startswith("~"):
        raise ProfileError(f"{where} 必须使用相对于项目根目录的 POSIX 路径：{raw!r}")
    if re.match(r"^[A-Za-z]:", raw):
        raise ProfileError(f"{where} 不能是绝对 Windows 路径：{raw!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise ProfileError(f"{where} 的路径越出了项目根目录：{raw!r}")
    if not allow_dot and (raw == "." or not rel.parts):
        raise ProfileError(f"{where} 必须指向项目根目录下的路径")
    if not allow_glob and any(ch in raw for ch in "*?[]"):
        raise ProfileError(f"{where} 必须是具体路径，不能是 glob 模式：{raw!r}")

    resolved_root = root.resolve()
    _reject_symlink_components(resolved_root, rel.parts, where)
    candidate = resolved_root.joinpath(*rel.parts).resolve(strict=False)
    if allow_glob and any(ch in raw for ch in "*?[]"):
        _reject_symlink_glob_matches(resolved_root, candidate, where)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ProfileError(f"{where} 解析到了项目根目录之外：{raw!r}") from exc
    return candidate


def _argv(value: object, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        # 单个可执行文件名可以直接写成字符串；命令始终按 argv 执行，不运行 shell 字符串。
        command = value.strip()
        if not command or any(ch.isspace() for ch in command):
            raise ProfileError(f"{where} 必须是 argv 数组；不接受 shell 命令字符串")
        return (command,)
    command = _strings(value, where)
    if not command:
        raise ProfileError(f"{where} 不能为空")
    return command


def _string_table(value: object, where: str) -> Mapping[str, str]:
    table = _table(value, where)
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in table.items()):
        raise ProfileError(f"{where} 必须是字符串到字符串的映射")
    return MappingProxyType(dict(table))


def load_profile(config_path: str | Path) -> FlowConfig:
    """解析 TOML 配置，解析安全路径，并校验阶段依赖图。"""
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ProfileError(f"配置文件不存在：{path}")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError(f"无法读取配置文件 {path}：{exc}") from exc

    extras = sorted(set(raw) - _TOP_KEYS)
    if extras:
        raise ProfileError(f"配置文件包含不支持的顶层键：{', '.join(extras)}")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(f"schema_version 必须为 {SUPPORTED_SCHEMA_VERSION}")

    root = path.parent.resolve()
    project = _table(raw.get("project"), "project", allowed=_PROJECT_KEYS)
    project_name = _string(project.get("name"), "project.name")
    description = project.get("description", "")
    if not isinstance(description, str):
        raise ProfileError("project.description 必须是字符串")

    flow = _table(raw.get("flow", {}), "flow", allowed=_FLOW_KEYS)
    output_dir = _relative_path(
        flow.get("output_dir", DEFAULT_OUTPUT_DIR),
        "flow.output_dir",
        root,
        allow_glob=False,
    )
    default_target_value = flow.get("default_target")
    default_target = None if default_target_value is None else _string(default_target_value, "flow.default_target")
    max_parallel_stages = flow.get("max_parallel_stages", 1)
    if type(max_parallel_stages) is not int or max_parallel_stages < 1:
        raise ProfileError("flow.max_parallel_stages 必须是大于或等于 1 的整数")

    raw_toolchains = _table(raw.get("toolchains", {}), "toolchains")
    toolchains: dict[str, ToolchainConfig] = {}
    for name, value in raw_toolchains.items():
        if not name.strip():
            raise ProfileError("工具链名称不能为空")
        definition = _table(value, f"toolchains.{name}", allowed=_TOOLCHAIN_KEYS)
        command = _argv(definition.get("command"), f"toolchains.{name}.command")
        version_args = _strings(
            definition.get("version_args", ["--version"]),
            f"toolchains.{name}.version_args",
        )
        expected_version_value = definition.get("expected_version")
        expected_version = (
            None
            if expected_version_value is None
            else _string(expected_version_value, f"toolchains.{name}.expected_version")
        )
        version_command_value = definition.get("version_command")
        version_command = (
            None
            if version_command_value is None
            else _strings(version_command_value, f"toolchains.{name}.version_command")
        )
        if version_command is not None and not version_command:
            raise ProfileError(f"toolchains.{name}.version_command 不能为空")
        failure_markers = _strings(definition.get("failure_markers", []), f"toolchains.{name}.failure_markers")
        toolchains[name] = ToolchainConfig(
            name, command, version_args, expected_version, version_command, failure_markers,
        )

    raw_targets = _table(raw.get("targets", {}), "targets")
    targets: dict[str, Mapping[str, Any]] = {}
    for name, value in raw_targets.items():
        if not name.strip():
            raise ProfileError("目标板名称不能为空")
        targets[name] = MappingProxyType(dict(_table(value, f"targets.{name}")))
    if default_target is not None and default_target not in targets:
        raise ProfileError(f"flow.default_target 引用了未知目标 {default_target!r}")

    raw_external = _table(raw.get("external_dependencies", {}), "external_dependencies")
    external_dependencies: dict[str, ExternalDependencyConfig] = {}
    for name, value in raw_external.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ProfileError(f"外部依赖名称无效：{name!r}")
        definition = _table(value, f"external_dependencies.{name}",
                            allowed={"path", "revision", "inputs", "environment_variable", "ignored_committed_paths"})
        path_text = _string(definition.get("path"), f"external_dependencies.{name}.path")
        if chr(0) in path_text:
            raise ProfileError(f"external_dependencies.{name}.path 不能包含 NUL")
        path_value = Path(path_text).expanduser()
        external_path = (path_value if path_value.is_absolute() else root / path_value).resolve(strict=False)
        revision = _string(definition.get("revision"), f"external_dependencies.{name}.revision").lower()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ProfileError(f"external_dependencies.{name}.revision 必须是完整 Git 提交标识")
        patterns = _strings(definition.get("inputs"), f"external_dependencies.{name}.inputs")
        if not patterns:
            raise ProfileError(f"external_dependencies.{name}.inputs 不能为空")
        ignored_paths = _strings(definition.get("ignored_committed_paths", []), f"external_dependencies.{name}.ignored_committed_paths")
        for pattern in (*patterns, *ignored_paths):
            rel = PurePosixPath(pattern)
            if rel.is_absolute() or ".." in rel.parts or "\\" in pattern or chr(0) in pattern:
                raise ProfileError(f"外部依赖输入必须是仓库内的 POSIX 路径：{pattern!r}")
        variable = definition.get("environment_variable")
        if variable is not None and (not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable)):
            raise ProfileError(f"external_dependencies.{name}.environment_variable 无效")
        external_dependencies[name] = ExternalDependencyConfig(name, external_path, revision, patterns, variable, ignored_paths)

    bundle: BundleConfig | None = None
    raw_bundle = raw.get("bundle")
    if raw_bundle is not None:
        bundle_table = _table(raw_bundle, "bundle", allowed={"schema_version", "output_dir", "artifacts", "dependencies"})
        bundle_schema = bundle_table.get("schema_version", 3)
        if type(bundle_schema) is not int or bundle_schema != 3:
            raise ProfileError("bundle.schema_version 必须为 3")
        bundle_output = _relative_path(
            bundle_table.get("output_dir", "out/current"),
            "bundle.output_dir",
            root,
            allow_glob=False,
        )
        raw_artifacts = _table(bundle_table.get("artifacts"), "bundle.artifacts")
        artifacts: dict[str, str] = {}
        for artifact_name, artifact_path in raw_artifacts.items():
            if not isinstance(artifact_name, str) or not artifact_name.strip():
                raise ProfileError("bundle.artifacts 名称不能为空")
            artifacts[artifact_name] = _relative_path(
                artifact_path,
                f"bundle.artifacts.{artifact_name}",
                root,
                allow_glob=False,
            ).relative_to(root).as_posix()
        if not artifacts:
            raise ProfileError("bundle.artifacts 至少需要一个产物")
        dependencies = _string_table(
            bundle_table.get("dependencies", {}),
            "bundle.dependencies",
        )
        bundle = BundleConfig(
            schema_version=bundle_schema,
            output_dir=bundle_output,
            artifacts=MappingProxyType(artifacts),
            dependencies=dependencies,
        )

    raw_stages = _table(raw.get("stages"), "stages")
    if not raw_stages:
        raise ProfileError("stages 至少需要包含一个阶段")
    stages: dict[str, StageConfig] = {}
    for name, value in raw_stages.items():
        if not name.strip():
            raise ProfileError("阶段名称不能为空")
        definition = _table(value, f"stages.{name}", allowed=_STAGE_KEYS)
        kind = definition.get("kind")
        if kind not in STAGE_KINDS:
            raise ProfileError(
                f"stages.{name}.kind 必须是以下值之一：{', '.join(sorted(STAGE_KINDS))}"
            )
        runner_value = definition.get("runner", "command")
        if not isinstance(runner_value, str) or runner_value not in STAGE_RUNNERS:
            raise ProfileError(
                f"stages.{name}.runner 必须是以下值之一：{', '.join(sorted(STAGE_RUNNERS))}"
            )
        runner = runner_value

        needs = _strings(definition.get("needs", []), f"stages.{name}.needs")
        inputs = tuple(
            _relative_path(item, f"stages.{name}.inputs", root, allow_glob=True).relative_to(root).as_posix()
            for item in _strings(definition.get("inputs", []), f"stages.{name}.inputs")
        )
        outputs = tuple(
            _relative_path(item, f"stages.{name}.outputs", root, allow_glob=True).relative_to(root).as_posix()
            for item in _strings(definition.get("outputs", []), f"stages.{name}.outputs")
        )
        command_value = definition.get("command")
        if runner == "command":
            command = _argv(command_value, f"stages.{name}.command")
        else:
            command = () if command_value is None else _argv(command_value, f"stages.{name}.command")
        options = MappingProxyType(dict(_table(definition.get("options", {}), f"stages.{name}.options")))
        cwd = _relative_path(definition.get("cwd", "."), f"stages.{name}.cwd", root,
                             allow_glob=False, allow_dot=True)
        toolchain_names = _strings(definition.get("toolchains", []), f"stages.{name}.toolchains")
        env = _string_table(definition.get("env", {}), f"stages.{name}.env")
        timeout_value = definition.get("timeout_seconds")
        if timeout_value is not None and (type(timeout_value) is not int or timeout_value <= 0):
            raise ProfileError(f"stages.{name}.timeout_seconds 必须是正整数")
        target_value = definition.get("target")
        target = None if target_value is None else _string(target_value, f"stages.{name}.target")
        if target is not None and target not in targets:
            raise ProfileError(f"stages.{name}.target 引用了未知目标 {target!r}")
        destructive = definition.get("destructive", False)
        if type(destructive) is not bool:
            raise ProfileError(f"stages.{name}.destructive 必须是布尔值")
        stage_description = definition.get("description", "")
        if not isinstance(stage_description, str):
            raise ProfileError(f"stages.{name}.description 必须是字符串")

        stages[name] = StageConfig(
            name=name,
            kind=kind,
            needs=needs,
            inputs=inputs,
            outputs=outputs,
            command=command,
            cwd=cwd,
            toolchains=toolchain_names,
            env=env,
            timeout_seconds=timeout_value,
            success_markers=_strings(definition.get("success_markers", []), f"stages.{name}.success_markers"),
            failure_markers=_strings(definition.get("failure_markers", []), f"stages.{name}.failure_markers"),
            side_effects=_strings(definition.get("side_effects", []), f"stages.{name}.side_effects"),
            resources=_strings(definition.get("resources", []), f"stages.{name}.resources"),
            target=target,
            destructive=destructive,
            description=stage_description,
            runner=runner,
            options=options,
            external_inputs=_strings(definition.get("external_inputs", []), f"stages.{name}.external_inputs"),
        )

    for name, stage in stages.items():
        for dependency in stage.needs:
            if dependency not in stages:
                raise ProfileError(f"stages.{name}.needs 引用了未知阶段 {dependency!r}")
        for toolchain in stage.toolchains:
            if toolchain not in toolchains:
                raise ProfileError(f"stages.{name}.toolchains 引用了未知工具链 {toolchain!r}")
        for dependency in stage.external_inputs:
            if dependency not in external_dependencies:
                raise ProfileError(f"stages.{name}.external_inputs 引用了未知依赖 {dependency!r}")

    raw_suites = _table(raw.get("suites", {}), "suites")
    suites: dict[str, tuple[str, ...]] = {}
    for suite_name, value in raw_suites.items():
        if not suite_name.strip():
            raise ProfileError("测试套件名称不能为空")
        suite_stages = _strings(value, f"suites.{suite_name}")
        for stage_name in suite_stages:
            if stage_name not in stages:
                raise ProfileError(f"suites.{suite_name} 引用了未知阶段 {stage_name!r}")
            if stages[stage_name].kind not in {"test", "validate"}:
                raise ProfileError(
                    f"suites.{suite_name} 阶段 {stage_name!r} 的 kind 必须是 test 或 validate"
                )
        suites[suite_name] = suite_stages

    config = FlowConfig(
        schema_version=schema_version,
        project_name=project_name,
        root=root,
        config_path=path,
        output_dir=output_dir,
        max_parallel_stages=max_parallel_stages,
        default_target=default_target,
        toolchains=MappingProxyType(toolchains),
        targets=MappingProxyType(targets),
        stages=MappingProxyType(stages),
        suites=MappingProxyType(suites),
        bundle=bundle,
        external_dependencies=MappingProxyType(external_dependencies),
    )
    # 校验所有阶段，包括当前选择的套件无法到达的循环依赖。
    topological_order(config, tuple(stages))
    try:
        artifact_producers(config)
    except BundleError as exc:
        raise ProfileError(str(exc)) from exc
    return config


def topological_order(config: FlowConfig, selected_stages: Sequence[str]) -> list[str]:
    """按稳定的依赖顺序返回选定阶段及其前置阶段。"""
    ordered: list[str] = []
    permanent: set[str] = set()
    temporary: list[str] = []

    def visit(name: str) -> None:
        if name not in config.stages:
            raise ProfileError(f"未知阶段 {name!r}")
        if name in permanent:
            return
        if name in temporary:
            start = temporary.index(name)
            cycle = temporary[start:] + [name]
            raise ProfileError("发现阶段循环依赖：" + " -> ".join(cycle))
        temporary.append(name)
        for dependency in config.stages[name].needs:
            visit(dependency)
        temporary.pop()
        permanent.add(name)
        ordered.append(name)

    for selected in selected_stages:
        visit(selected)
    return ordered


def load_json_schema() -> dict[str, Any]:
    """从已安装的包资源中读取 JSON Schema。"""
    import json
    from importlib.resources import files

    schema_resource = files("fpga_flow").joinpath("schema", "flow-v1.schema.json")
    try:
        return json.loads(schema_resource.read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError) as exc:
        raise ProfileError(f"已安装的配置 Schema 缺失：{exc}") from exc
