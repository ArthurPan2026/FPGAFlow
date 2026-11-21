"""Built-in execution profiles for declarative FPGA project descriptors."""
from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
from pathlib import Path
import tomllib
from typing import Any

from .project import ProjectDescriptor, ProjectError, project_stage_inputs


def _expand_project_inputs(project: ProjectDescriptor, stage_name: str, patterns: list[str]) -> list[str]:
    expanded: list[str] = []
    for pattern in patterns:
        if not pattern.startswith("@project."):
            expanded.append(pattern)
            continue
        selector = pattern.removeprefix("@project.")
        if selector in {"hardware", "package", "rtl_project", "sim", "rtl"}:
            expanded.extend(project_stage_inputs(project, selector))
            continue
        if selector in {"application", "platform"}:
            expanded.extend(project_stage_inputs(project, "app" if selector == "application" else "platform"))
            continue
        if selector.startswith("tests:"):
            suite = selector.partition(":")[2]
            expanded.extend(project_stage_inputs(project, f"rtl_{suite}"))
            continue
        raise ProjectError(f"profile stage {stage_name!r} has unknown input selector {pattern!r}")
    return list(dict.fromkeys(expanded))


def _rewrite_output_root(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old + "/"):
            return new + value[len(old):]
        return value
    if isinstance(value, list):
        return [_rewrite_output_root(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_output_root(item, old, new) for key, item in value.items()}
    return value


def _replace_string_token(value: Any, token: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, replacement)
    if isinstance(value, list):
        return [_replace_string_token(item, token, replacement) for item in value]
    if isinstance(value, dict):
        return {key: _replace_string_token(item, token, replacement) for key, item in value.items()}
    return value


def load_project_profile(project: ProjectDescriptor) -> dict[str, Any]:
    profile_path = files("fpga_flow").joinpath("profiles", f"{project.profile}.toml")
    try:
        raw = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"cannot load built-in profile {project.profile!r}: {exc}") from exc
    raw = deepcopy(raw)
    package_version = project.package_version
    if package_version is None and project.dependencies:
        package_version = next(iter(project.dependencies.values()))["version"]
    if package_version:
        raw = _replace_string_token(raw, "{package_version}", package_version)
    for name, version in project.tool_versions.items():
        raw = _replace_string_token(raw, "{" + name + "}", version)
    raw_project = raw.get("project")
    if not isinstance(raw_project, dict):
        raise ProjectError(f"built-in profile {project.profile!r} has no project table")
    raw_project["name"] = project.name
    raw_project["description"] = f"Built-in {project.profile} profile"

    output_relative = project.output_dir.relative_to(project.root).as_posix()
    flow = raw.setdefault("flow", {})
    flow["output_dir"] = output_relative
    if project.board:
        target_id = str(project.board.get("id") or flow.get("default_target") or "")
        targets = raw.setdefault("targets", {})
        if target_id not in targets:
            template = next(iter(targets.values()), {})
            targets = {target_id: deepcopy(template)}
            raw["targets"] = targets
            for stage in raw.get("stages", {}).values():
                if "target" in stage:
                    stage["target"] = target_id
        flow["default_target"] = target_id
        for key in ("family", "part", "board_part", "device", "board_id",
                    "flash_type", "flash_size_bytes", "target_device_name", "target_idcode", "pl_target_pattern"):
            if key in project.board:
                targets[target_id][key] = project.board[key]
    unknown_adapters = set(project.adapters) - set(raw.get("stages", {}))
    if unknown_adapters:
        raise ProjectError(f"unknown project adapters: {sorted(unknown_adapters)}")

    for stage_name, stage in raw.get("stages", {}).items():
        if not isinstance(stage, dict):
            continue
        adapter = project.adapters.get(stage_name, {})
        if not isinstance(adapter, dict):
            raise ProjectError(f"adapters.{stage_name} must be a table")
        if stage.get("runner", "command") == "command" and not adapter.get("command"):
            raise ProjectError(f"adapters.{stage_name}.command is required; Flow does not discover scripts")
        allowed = {"command", "inputs", "outputs", "success_markers", "options"}
        if set(adapter) - allowed:
            raise ProjectError(f"adapters.{stage_name} has unsupported keys")
        for key, value in adapter.items():
            if key == "inputs":
                stage[key] = [*stage.get(key, []), *deepcopy(value)]
            else:
                stage[key] = deepcopy(value)
        if "inputs" in stage:
            stage["inputs"] = _expand_project_inputs(project, stage_name, stage["inputs"])
    for name, stage in project.validation.get("stages", {}).items():
        if not isinstance(stage, dict):
            raise ProjectError(f"validation.stages.{name} must be a table")
        if name in raw.get("stages", {}):
            raise ProjectError(f"project validation may not replace built-in stage {name}")
        if stage.get("kind") != "validate":
            raise ProjectError(f"project validation stage {name} must have kind=validate")
        raw.setdefault("stages", {})[name] = deepcopy(stage)
    raw.setdefault("suites", {}).update(deepcopy(project.validation.get("suites", {})))
    if project.dependencies and isinstance(raw.get("bundle"), dict):
        raw["bundle"]["dependencies"] = {
            name: value["version"] for name, value in project.dependencies.items()
        }
    if project.board and isinstance(raw.get("bundle"), dict):
        raw["bundle"].setdefault("dependencies", {})["board_profile"] = str(
            project.board.get("id") or project.board.get("part") or ""
        )
    if package_version:
        raw = _replace_string_token(raw, "{package_version}", package_version)
    for name, version in project.tool_versions.items():
        raw = _replace_string_token(raw, "{" + name + "}", version)
    return _rewrite_output_root(raw, "build/fpga-flow", output_relative)
