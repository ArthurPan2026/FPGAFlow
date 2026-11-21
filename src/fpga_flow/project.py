"""Declarative FPGA project inputs and generated source/test inventory."""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from .fingerprint import sha256_file


PROJECT_SCHEMA_VERSION = 1
_PROJECT_KEYS = {"id", "name", "profile"}
_TOP_KEYS = {"schema_version", "project", "board", "vivado", "vitis", "tools", "ip_package", "dependencies", "tests", "build", "exports", "validation", "adapters"}
_PROFILES = {"zynq7000-vitis", "vivado-ip-package"}
_HDL_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".mem", ".coe", ".mif"}


class ProjectError(ValueError):
    """Invalid project descriptor or unresolved project source."""


@dataclass(frozen=True)
class ProjectDescriptor:
    path: Path
    root: Path
    project_id: str
    name: str
    profile: str
    output_dir: Path
    board: Mapping[str, Any]
    tool_versions: Mapping[str, str]
    vivado_project: str | None
    ip_components: tuple[str, ...]
    ip_metadata: tuple[str, ...]
    vitis_app_config: str | None
    software_source_roots: tuple[str, ...]
    package_component_xml: str | None
    package_version_file: str | None
    package_version: str | None
    package_source_roots: tuple[str, ...]
    tests: tuple[Mapping[str, Any], ...]
    test_source_roots: tuple[str, ...]
    suite_stages: Mapping[str, str]
    dependencies: Mapping[str, Mapping[str, Any]]
    export_dir: str | None
    export_artifacts: tuple[str, ...]
    validation: Mapping[str, Any]
    adapters: Mapping[str, Any]


def _table(value: Any, where: str, allowed: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectError(f"{where} must be a TOML table")
    extra = sorted(set(value) - allowed) if allowed is not None else []
    if extra:
        raise ProjectError(f"{where} contains unsupported keys: {', '.join(extra)}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError(f"{where} must be a non-empty string")
    return value.strip()


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProjectError(f"{where} must be an array of non-empty strings")
    result = tuple(item.strip().replace("\\", "/") for item in value)
    if len(set(result)) != len(result):
        raise ProjectError(f"{where} contains duplicates")
    return result


def _relative(root: Path, value: Any, where: str, *, must_exist: bool = True) -> str:
    text = _string(value, where).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or "\x00" in text:
        raise ProjectError(f"{where} must remain inside the project: {text!r}")
    candidate = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ProjectError(f"{where} contains a symlink: {current}")
    if must_exist and not candidate.exists():
        raise ProjectError(f"{where} does not exist: {text}")
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ProjectError(f"{where} escapes the project or cannot be resolved: {text}") from exc
    return path.as_posix()


def load_project(path: str | Path) -> ProjectDescriptor:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ProjectError(f"project descriptor does not exist: {manifest_path}")
    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectError(f"cannot read project descriptor {manifest_path}: {exc}") from exc
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ProjectError(f"project descriptor contains unsupported tables: {', '.join(unknown)}")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != PROJECT_SCHEMA_VERSION:
        raise ProjectError(f"schema_version must be {PROJECT_SCHEMA_VERSION}")

    root = manifest_path.parent.resolve(strict=True)
    project = _table(raw.get("project"), "project", _PROJECT_KEYS)
    project_id = _string(project.get("id"), "project.id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", project_id):
        raise ProjectError("project.id must be a short stable identifier")
    name = _string(project.get("name"), "project.name")
    profile = _string(project.get("profile"), "project.profile")
    if profile not in _PROFILES:
        raise ProjectError(f"unknown built-in project profile: {profile}")

    build = _table(raw.get("build", {}), "build", {"output_dir"})
    output_dir = root / _relative(root, build.get("output_dir", "build/fpga-flow"), "build.output_dir", must_exist=False)
    board = _table(raw.get("board", {}), "board",
        {"id", "family", "part", "board_part", "device", "board_id",
         "flash_type", "flash_size_bytes", "target_device_name", "target_idcode", "pl_target_pattern"})
    tools = _table(raw.get("tools", {}), "tools", {"vivado_version", "vitis_version"})
    tool_versions = {
        key: _string(value, f"tools.{key}")
        for key, value in tools.items()
    }

    vivado_project: str | None = None
    ip_components: tuple[str, ...] = ()
    if profile == "zynq7000-vitis":
        vivado = _table(raw.get("vivado"), "vivado", {"project"})
        vivado_project = _relative(root, vivado.get("project"), "vivado.project")
        if Path(vivado_project).suffix.lower() != ".xpr":
            raise ProjectError("vivado.project must identify the canonical .xpr file")
        vitis = _table(raw.get("vitis"), "vitis", {"app_config", "source_roots"})
        vitis_app_config = _relative(root, vitis.get("app_config"), "vitis.app_config")
        software_source_roots = tuple(_relative(root, item, "vitis.source_roots[]") for item in
                                      _strings(vitis.get("source_roots"), "vitis.source_roots"))
        ip_components = ()
        ip_metadata = ()
        package_component_xml = None
        package_version_file = None
        package_version = None
        package_source_roots = ()
        dependencies_raw = _table(raw.get("dependencies", {}), "dependencies")
        dependencies: dict[str, Mapping[str, Any]] = {}
        for dependency_name, dependency_value in dependencies_raw.items():
            dependency = _table(
                dependency_value, f"dependencies.{dependency_name}",
                {"version", "package_dir", "component_xml", "metadata", "provenance"},
            )
            package_dir = _relative(root, dependency.get("package_dir"), f"dependencies.{dependency_name}.package_dir")
            component_xml_value = dependency.get("component_xml")
            component_xml = (
                _relative(root, component_xml_value, f"dependencies.{dependency_name}.component_xml")
                if component_xml_value is not None else None
            )
            metadata = _strings(dependency.get("metadata", []), f"dependencies.{dependency_name}.metadata")
            dependencies[dependency_name] = {
                "version": _string(dependency.get("version"), f"dependencies.{dependency_name}.version"),
                "package_dir": package_dir,
                "component_xml": component_xml,
                "metadata": tuple(
                    _relative(root, item, f"dependencies.{dependency_name}.metadata[]")
                    for item in metadata
                ),
                "provenance": dict(_table(dependency.get("provenance", {}),
                    f"dependencies.{dependency_name}.provenance")),
            }
            if component_xml is not None:
                ip_components += (component_xml,)
            ip_metadata += tuple(
                _relative(root, item, f"dependencies.{dependency_name}.metadata[]")
                for item in metadata
            )
    else:
        package = _table(raw.get("ip_package"), "ip_package", {"component_xml", "version_file", "source_roots"})
        package_component_xml = _relative(
            root, package.get("component_xml"), "ip_package.component_xml", must_exist=False
        )
        package_version_file = _relative(root, package.get("version_file"), "ip_package.version_file")
        package_version = _string(
            (root / package_version_file).read_text(encoding="utf-8").strip(),
            "ip_package.version_file contents",
        )
        package_source_roots = tuple(
            _relative(root, item, "ip_package.source_roots[]")
            for item in _strings(package.get("source_roots"), "ip_package.source_roots")
        )
        ip_components = (package_component_xml,)
        ip_metadata = (package_version_file,)
        vitis_app_config = None
        software_source_roots = ()
        dependencies = {}

    tests = _table(
        raw.get("tests"),
        "tests",
        {"source_roots", "suite_stages", "default_success_markers", "bench"},
    )
    test_source_roots = tuple(
        _relative(root, item, "tests.source_roots[]")
        for item in _strings(tests.get("source_roots"), "tests.source_roots")
    )
    suite_stages = _table(tests.get("suite_stages", {}), "tests.suite_stages")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in suite_stages.items()):
        raise ProjectError("tests.suite_stages must map suite names to Flow stage names")
    default_markers = _strings(
        tests.get("default_success_markers", ["TEST_PASS"]),
        "tests.default_success_markers",
    )
    if not default_markers:
        raise ProjectError("tests.default_success_markers must not be empty")
    test_entries = _parse_test_entries(root, tests.get("bench"), suite_stages, default_markers)

    exports = _table(raw.get("exports", {}), "exports", {"directory", "artifacts"})
    export_dir = None
    export_artifacts: tuple[str, ...] = ()
    if exports:
        export_dir = _relative(root, exports.get("directory"), "exports.directory", must_exist=False)
        export_artifacts = _strings(exports.get("artifacts"), "exports.artifacts")
        if not export_artifacts:
            raise ProjectError("exports.artifacts must be non-empty when exports is configured")

    validation = _table(raw.get("validation", {}), "validation", {"stages", "suites"})
    _table(validation.get("stages", {}), "validation.stages")
    _table(validation.get("suites", {}), "validation.suites")
    adapters = _table(raw.get("adapters", {}), "adapters")
    for adapter_name, adapter in adapters.items():
        _table(adapter, f"adapters.{adapter_name}",
            {"command", "inputs", "outputs", "success_markers", "options"})
        for key in ("command", "inputs", "outputs", "success_markers"):
            if key in adapter:
                _strings(adapter[key], f"adapters.{adapter_name}.{key}")
        if "options" in adapter:
            _table(adapter["options"], f"adapters.{adapter_name}.options")

    return ProjectDescriptor(
        path=manifest_path,
        root=root,
        project_id=project_id,
        name=name,
        profile=profile,
        output_dir=output_dir,
        board=dict(board),
        tool_versions=tool_versions,
        vivado_project=vivado_project,
        ip_components=ip_components,
        ip_metadata=ip_metadata,
        vitis_app_config=vitis_app_config,
        software_source_roots=software_source_roots,
        package_component_xml=package_component_xml,
        package_version_file=package_version_file,
        package_version=package_version,
        package_source_roots=package_source_roots,
        tests=test_entries,
        test_source_roots=test_source_roots,
        suite_stages={str(key): str(value) for key, value in suite_stages.items()},
        dependencies=dependencies,
        export_dir=export_dir,
        export_artifacts=export_artifacts,
        validation=dict(validation),
        adapters=dict(adapters),
    )


def default_state_dir(project: ProjectDescriptor) -> Path:
    """Return a host-local state directory that cannot collide across checkouts."""
    configured = os.environ.get("FPGA_FLOW_STATE_ROOT")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "FPGAFlow"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "fpga-flow"
    checkout_id = hashlib.sha256(str(project.root).encode("utf-8")).hexdigest()[:16]
    return (root / "projects" / f"{project.project_id}-{checkout_id}").resolve(strict=False)


def _local(element: str) -> str:
    return element.rsplit("}", 1)[-1]


def _resolve_xpr_path(root: Path, xpr: Path, raw_path: str, *, require_file: bool = True) -> str:
    project_name = xpr.stem
    replacements = {
        "$PPRDIR": xpr.parent,
        "$PSRCDIR": xpr.parent / f"{project_name}.srcs",
        "$PGENDIR": xpr.parent / f"{project_name}.gen",
    }
    resolved_text = raw_path
    for token, value in replacements.items():
        resolved_text = resolved_text.replace(token, str(value))
    if "$" in resolved_text:
        raise ProjectError(f"unsupported Vivado path variable in XPR: {raw_path}")
    candidate = Path(resolved_text)
    if not candidate.is_absolute():
        candidate = xpr.parent / candidate
    lexical = Path(os.path.normpath(candidate))
    try:
        lexical_relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ProjectError(f"XPR source escapes the project: {raw_path}") from exc
    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ProjectError(f"XPR source traverses a symlink: {current}")
    try:
        canonical = candidate.resolve(strict=require_file)
        rel = canonical.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ProjectError(f"XPR source is missing or outside the project: {raw_path}") from exc
    if require_file and not canonical.is_file():
        raise ProjectError(f"XPR source is not a regular file: {raw_path}")
    if not require_file and not canonical.is_dir():
        raise ProjectError(f"XPR IP repository is not a directory: {raw_path}")
    return rel.as_posix()


def _component_sources(root: Path, component_path: str) -> list[tuple[str, str]]:
    component = root / component_path
    try:
        tree = ET.parse(component)
    except (OSError, ET.ParseError) as exc:
        raise ProjectError(f"cannot parse IP component metadata {component_path}: {exc}") from exc
    result: set[tuple[str, str]] = set()
    for fileset in tree.iter():
        if _local(fileset.tag) != "fileSet":
            continue
        set_name = next((child.text or "" for child in fileset if _local(child.tag) == "name"), "").lower()
        role = "ip_simulation" if "simulation" in set_name else "ip_source"
        for item in fileset:
            if _local(item.tag) != "file":
                continue
            for child in item:
                if _local(child.tag) not in {"name", "fileName"} or not child.text:
                    continue
                value = child.text.strip()
                if "$" in value:
                    raise ProjectError(f"unresolved IP component path: {component_path}: {value}")
                relative = _resolve_xpr_path(root, component, value)
                result.add((relative, role))
    return [(component_path, "ip_source"), *sorted(result)]


def xpr_sources(project: ProjectDescriptor) -> list[dict[str, Any]]:
    if project.vivado_project is None:
        return []
    xpr = project.root / project.vivado_project
    try:
        tree = ET.parse(xpr)
    except (OSError, ET.ParseError) as exc:
        raise ProjectError(f"cannot parse Vivado project {project.vivado_project}: {exc}") from exc
    result: dict[str, dict[str, Any]] = {
        project.vivado_project: {
            "path": project.vivado_project, "role": "vivado_project",
            "fileset": "project", "active": True, "used_in": (),
        }
    }
    for fileset in tree.iter():
        if _local(fileset.tag) != "FileSet":
            continue
        fileset_name = str(fileset.attrib.get("Name", "unknown"))
        fileset_type = str(fileset.attrib.get("Type", ""))
        if fileset_type.casefold() == "utils":
            continue
        role = "constraint" if fileset_type.casefold() == "constrs" else (
            "simulation" if fileset_type.casefold() == "simulationsrcs" else "design"
        )
        for entry in fileset:
            if _local(entry.tag) != "File":
                continue
            raw_path = entry.attrib.get("Path")
            if not raw_path:
                continue
            relative = _resolve_xpr_path(project.root, xpr, raw_path)
            attrs = {
                child.attrib.get("Name"): child.attrib.get("Val", "")
                for child in entry.iter()
                if _local(child.tag) == "Attr"
            }
            active = attrs.get("AutoDisabled") != "1"
            used_in = tuple(sorted({
                child.attrib.get("Val", "")
                for child in entry.iter()
                if _local(child.tag) == "Attr"
                and child.attrib.get("Name") == "UsedIn"
                and child.attrib.get("Val")
            }))
            result[relative] = {
                "path": relative, "role": role, "fileset": fileset_name,
                "active": active, "used_in": used_in,
            }
            if Path(relative).suffix.lower() == ".bd":
                bd = project.root / relative
                try:
                    content = json.loads(bd.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ProjectError(f"cannot parse Block Design {relative}: {exc}") from exc
                pending = [content]
                while pending:
                    node = pending.pop()
                    if isinstance(node, list):
                        pending.extend(node)
                    elif isinstance(node, dict):
                        pending.extend(node.values())
                        reference = node.get("xci_path")
                        if isinstance(reference, str):
                            path = _resolve_xpr_path(project.root, bd, reference)
                            result[path] = {"path": path, "role": "design",
                                "fileset": fileset_name, "active": active,
                                "used_in": ("synthesis", "implementation")}

    for component in project.ip_components:
        for path, role in _component_sources(project.root, component):
            if role == "ip_simulation" and path in result and result[path]["role"] != "ip_simulation":
                continue
            result[path] = {
                "path": path, "role": role, "fileset": "ip_component",
                "active": True, "used_in": (("simulation",) if role == "ip_simulation" else ("synthesis", "implementation")),
            }
    for metadata in project.ip_metadata:
        result[metadata] = {
            "path": metadata, "role": "ip_metadata", "fileset": "project_manifest",
            "active": True, "used_in": ("ip_catalog",),
        }
    for entry in tree.iter():
        for key, value in entry.attrib.items():
            if key.endswith("TclHook") and value:
                path = _resolve_xpr_path(project.root, xpr, value)
                result[path] = {"path": path, "role": "design", "fileset": "run_hook",
                    "active": True, "used_in": ("synthesis", "implementation")}
    root = project.root
    for option in tree.iter():
        if _local(option.tag) != "Option" or option.attrib.get("Name") != "IPRepoPath":
            continue
        for raw_repo in option.attrib.get("Val", "").split(";"):
            raw_repo = raw_repo.strip()
            if not raw_repo:
                continue
            repo = _resolve_xpr_path(root, xpr, raw_repo, require_file=False)
            declared_repositories = {
                f"{dependency['package_dir'].rstrip('/')}/ip_repo"
                for dependency in project.dependencies.values()
                if dependency["component_xml"] is not None
            }
            if declared_repositories and repo not in declared_repositories:
                raise ProjectError(
                    f"XPR IP repository {repo} does not match the project dependency declaration"
                )
            result[repo] = {
                "path": repo, "role": "ip_repository", "fileset": "vivado_project",
                "active": True, "used_in": ("ip_catalog",),
            }
    return [result[name] for name in sorted(result)]


def project_stage_inputs(project: ProjectDescriptor, stage_name: str) -> tuple[str, ...]:
    """Return exact project-owned inputs for a built-in profile stage."""
    inputs: set[str] = set()
    if stage_name in {"hardware", "rtl_project"}:
        for item in xpr_sources(project):
            if item["role"] == "vivado_project" or (
                item["active"] and (
                    item["role"] in {"design", "constraint", "ip_source", "ip_metadata"}
                    and (not item["used_in"] or bool(set(item["used_in"]) &
                        {"synthesis", "implementation", "ip_catalog"}))
                )
            ):
                if (project.root / item["path"]).is_file():
                    inputs.add(item["path"])
    elif stage_name in {"platform", "app"}:
        if project.vitis_app_config:
            inputs.add(project.vitis_app_config)
        if stage_name == "app":
            inputs.update(_software_sources(project))
    elif stage_name in {"package", "ip_catalog", "synth"}:
        inputs.update(_walk_declared_roots(project, project.package_source_roots))
        for item in xpr_sources(project):
            if item["role"] in {"ip_source", "ip_metadata"}:
                inputs.add(item["path"])
        if project.package_version_file:
            inputs.add(project.package_version_file)
    elif stage_name.startswith("rtl_") or stage_name in {"rtl", "sim"}:
        for test in _test_entries(project):
            if stage_name in {"rtl", "sim"} or test["stage"] == stage_name:
                inputs.update(test["sources"])
                inputs.update(test["iverilog_sources"])
    return tuple(sorted(inputs))


def project_context(project: ProjectDescriptor, stage_name: str) -> dict[str, Any]:
    """Return only project settings that affect the selected build stage."""
    common: dict[str, Any] = {"project_id": project.project_id, "profile": project.profile}
    if stage_name in {"hardware", "rtl_project", "ip_catalog", "package", "synth", "sim"} or stage_name.startswith("rtl_"):
        if "vivado_version" in project.tool_versions:
            common["vivado_version"] = project.tool_versions["vivado_version"]
    if stage_name in {"platform", "app", "boot"} and "vitis_version" in project.tool_versions:
        common["vitis_version"] = project.tool_versions["vitis_version"]
    if stage_name in {"hardware", "rtl_project", "deploy_jtag", "deploy_qspi", "deploy_qspi_release"}:
        common.update({
            "vivado_project": project.vivado_project,
            "board": dict(project.board),
            "dependencies": {
                name: {
                    "version": value["version"],
                    "package_dir": value["package_dir"],
                    "component_xml": value["component_xml"],
                }
                for name, value in project.dependencies.items()
            },
        })
    if stage_name in {"platform", "app", "boot"}:
        common.update({
            "vitis_app_config": project.vitis_app_config,
            "software_source_roots": list(project.software_source_roots),
            "dependencies": {
                name: {"version": value["version"], "package_dir": value["package_dir"]}
                for name, value in project.dependencies.items()
            },
        })
    if stage_name in {"package", "ip_catalog", "synth"}:
        common.update({
            "package_component_xml": project.package_component_xml,
            "package_version_file": project.package_version_file,
            "package_source_roots": list(project.package_source_roots),
        })
    if stage_name == "sim" or stage_name.startswith("rtl_"):
        common["tests"] = [
            {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in test.items()
            }
            for test in _test_entries(project)
            if test["stage"] == stage_name or stage_name in {"rtl", "sim"}
        ]
        common["suite_stages"] = dict(project.suite_stages)
    return common


def _test_entries(project: ProjectDescriptor) -> list[dict[str, Any]]:
    return [dict(entry) for entry in project.tests]


def _parse_test_entries(
    root: Path,
    entries: Any,
    suite_stages: Mapping[str, Any],
    default_markers: tuple[str, ...],
) -> tuple[Mapping[str, Any], ...]:
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ProjectError("tests.bench must be an array of tables")
    result: list[Mapping[str, Any]] = []
    names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ProjectError(f"test bench {index} must be a table")
        allowed = {
            "name", "suite", "top", "sources", "iverilog_sources", "iverilog",
            "defines", "success_markers", "timeout_seconds", "parallel", "resources", "artifacts",
        }
        extra = sorted(set(entry) - allowed)
        if extra:
            raise ProjectError(f"tests.bench[{index}] contains unsupported keys: {', '.join(extra)}")
        name = _string(entry.get("name"), f"tests.bench[{index}].name")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ProjectError(f"tests.bench[{index}].name is not a valid HDL identifier: {name}")
        if name.casefold() in names:
            raise ProjectError(f"duplicate test bench: {name}")
        names.add(name.casefold())
        sources: list[str] = []
        for source in _strings(entry.get("sources"), f"tests.bench[{index}].sources"):
            relative = _relative(root, source, f"tests.bench[{index}].sources")
            if Path(relative).suffix.lower() not in _HDL_SUFFIXES:
                raise ProjectError(f"test source is not HDL or memory data: {relative}")
            if relative not in sources:
                sources.append(relative)
        iverilog_sources: list[str] = []
        for source in _strings(entry.get("iverilog_sources"), f"tests.bench[{index}].iverilog_sources"):
            relative = _relative(root, source, f"tests.bench[{index}].iverilog_sources")
            if Path(relative).suffix.lower() not in _HDL_SUFFIXES:
                raise ProjectError(f"Icarus source is not HDL or memory data: {relative}")
            if relative not in iverilog_sources:
                iverilog_sources.append(relative)
        if not sources:
            raise ProjectError(f"tests.bench[{index}].sources must not be empty")
        suite = _string(entry.get("suite", "default"), f"tests.bench[{index}].suite")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", suite):
            raise ProjectError(f"tests.bench[{index}].suite is invalid: {suite}")
        top = _string(entry.get("top", name), f"tests.bench[{index}].top")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
            raise ProjectError(f"tests.bench[{index}].top is invalid: {top}")
        stage_name = suite_stages.get(suite) or suite_stages.get("default")
        markers = _strings(entry.get("success_markers", list(default_markers)), f"tests.bench[{index}].success_markers")
        if not markers:
            raise ProjectError(f"tests.bench[{index}].success_markers must not be empty")
        timeout = entry.get("timeout_seconds")
        if timeout is not None and (type(timeout) is not int or timeout <= 0):
            raise ProjectError(f"tests.bench[{index}].timeout_seconds must be a positive integer")
        parallel = entry.get("parallel", True)
        if type(parallel) is not bool:
            raise ProjectError(f"tests.bench[{index}].parallel must be a boolean")
        iverilog = entry.get("iverilog", bool(iverilog_sources))
        if type(iverilog) is not bool:
            raise ProjectError(f"tests.bench[{index}].iverilog must be a boolean")
        defines = _strings(entry.get("defines", []), f"tests.bench[{index}].defines")
        resources = _strings(entry.get("resources", []), f"tests.bench[{index}].resources")
        artifacts = tuple(
            _relative(root, item, f"tests.bench[{index}].artifacts[]", must_exist=False)
            for item in _strings(entry.get("artifacts", []), f"tests.bench[{index}].artifacts")
        )
        result.append({
            "name": name,
            "suite": suite,
            "top": top,
            "sources": tuple(sources),
            "iverilog_sources": tuple(iverilog_sources),
            "iverilog": iverilog,
            "defines": defines,
            "success_markers": markers,
            "timeout_seconds": timeout,
            "parallel": parallel,
            "resources": resources,
            "artifacts": artifacts,
            "stage": stage_name,
        })
    return tuple(result)


def _software_sources(project: ProjectDescriptor) -> list[str]:
    return _walk_declared_roots(project, project.software_source_roots)


def _test_source_files(project: ProjectDescriptor) -> list[str]:
    suffixes = _HDL_SUFFIXES | {".py", ".c", ".h", ".cpp", ".hpp", ".tcl", ".sh", ".toml"}
    return _walk_declared_roots(project, project.test_source_roots, suffixes=suffixes)


def _walk_declared_roots(
    project: ProjectDescriptor,
    roots: tuple[str, ...],
    *,
    suffixes: set[str] | None = None,
) -> list[str]:
    paths: set[str] = set()
    ignored_directories = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for raw_root in roots:
        directory = project.root / raw_root
        for current, directories, files in os.walk(directory, followlinks=False):
            current_path = Path(current)
            if any((current_path / name).is_symlink() for name in directories):
                raise ProjectError(f"declared source root traverses a symlink: {current_path}")
            directories[:] = sorted(name for name in directories if name not in ignored_directories)
            for name in sorted(files):
                candidate = current_path / name
                if candidate.is_symlink():
                    raise ProjectError(f"declared source root traverses a symlink: {candidate}")
                if candidate.is_file() and (suffixes is None or candidate.suffix.lower() in suffixes):
                    paths.add(candidate.relative_to(project.root).as_posix())
    return sorted(paths)


def _working_tree_status(root: Path, paths: set[str]) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, check=False,
        )
    except OSError:
        return {path: "unknown" for path in paths}
    if result.returncode != 0:
        return {path: "unknown" for path in paths}
    changed: dict[str, str] = {}
    entries = result.stdout.split(b"\x00")
    for entry in entries:
        if len(entry) < 4:
            continue
        try:
            state = entry[:2].decode("ascii", errors="replace").strip()
            path = entry[3:].decode("utf-8", errors="surrogateescape").replace("\\", "/")
        except (UnicodeDecodeError, IndexError):
            continue
        if state == "??":
            changed[path] = "untracked"
        elif "D" in state:
            changed[path] = "deleted"
        elif state:
            changed[path] = "modified"
    return {path: changed.get(path, "clean") for path in paths}


def inventory_data(
    project: ProjectDescriptor,
    stage_records: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    current_stage: str | None = None,
    current_status: str | None = None,
    current_run_id: str | None = None,
) -> dict[str, Any]:
    records: dict[str, Mapping[str, Any]] = dict(stage_records or {})
    sources = xpr_sources(project)
    sources.append({
        "path": project.path.relative_to(project.root).as_posix(),
        "role": "project_manifest", "fileset": "project",
        "active": True, "used_in": ("flow",),
    })
    if project.vitis_app_config:
        sources.append({
            "path": project.vitis_app_config, "role": "vitis_app_config",
            "fileset": "vitis", "active": True, "used_in": ("platform", "application"),
        })
    for path in _software_sources(project):
        sources.append({
            "path": path, "role": "software", "fileset": "vitis_source_root",
            "active": True, "used_in": ("application",),
        })
    for path in _walk_declared_roots(project, project.package_source_roots):
        sources.append({
            "path": path, "role": "package_source", "fileset": "ip_package_source_root",
            "active": True, "used_in": ("ip_packaging",),
        })
    if project.package_version_file:
        sources.append({
            "path": project.package_version_file, "role": "package_version",
            "fileset": "ip_package", "active": True, "used_in": ("ip_packaging",),
        })
    for path in _test_source_files(project):
        sources.append({
            "path": path, "role": "test_source", "fileset": "declared_test_roots",
            "active": True, "used_in": ("validation",),
        })
    # Explicit adapter inputs also include generated headers, build scripts and
    # host test fixtures that need not occur in a Vivado fileset.
    from .fingerprint import expand_inputs
    for stage_name, adapter in project.adapters.items():
        for pattern in adapter.get("inputs", []):
            if pattern.startswith("@project."):
                continue
            for path in expand_inputs(project.root, (pattern,), require_matches=True):
                sources.append({"path": path.relative_to(project.root).as_posix(),
                    "role": "adapter_input", "fileset": stage_name,
                    "active": True, "used_in": (stage_name,)})
    test_entries = _test_entries(project)
    for test in test_entries:
        role = "test_source"
        for path in test["sources"]:
            sources.append({
                "path": path, "role": role, "fileset": test["name"],
                "active": True, "used_in": ("simulation",),
            })
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for source in sources:
        unique[(source["path"], source["role"], source["fileset"])] = source
    sources = [unique[key] for key in sorted(unique)]
    paths = {source["path"] for source in sources}
    working_tree = _working_tree_status(project.root, paths)
    for source in sources:
        file_path = project.root / source["path"]
        source["sha256"] = sha256_file(file_path) if file_path.is_file() else ""
        source["working_tree"] = working_tree[source["path"]]

    tests: list[dict[str, Any]] = []
    for test in test_entries:
        stage_name = test["stage"]
        record = records.get(stage_name, {}) if stage_name else {}
        status = str(record.get("status", "not_run"))
        run_id = record.get("run_id")
        producer_run_id = record.get("producer_run_id") or run_id
        if stage_name == current_stage and current_status is not None:
            status = current_status
            run_id = current_run_id
            producer_run_id = current_run_id
        status = {"succeeded": "passed", "failed": "failed", "running": "running"}.get(status, "not_run")
        if status == "failed":
            # A suite failure cannot prove that every individual bench failed.
            status = "unknown"
        if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
            record_path = project.output_dir / "runs" / run_id / "run.json"
            if record_path.is_file():
                try:
                    full_record = json.loads(record_path.read_text(encoding="utf-8"))
                    producer_run_id = full_record.get("producer_run_id") or run_id
                except (OSError, ValueError):
                    status = "unknown"
            if not isinstance(producer_run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", producer_run_id):
                producer_run_id = run_id
                status = "unknown"
            run_root = project.output_dir / "runs" / producer_run_id
            if project.profile == "vivado-ip-package":
                bench_root = run_root / "work/tests/rtl" / test["name"]
                log_name = "run.log"
            else:
                bench_root = run_root / "work/tests/rtl" / test["suite"] / "seed-1/current" / test["name"]
                log_name = "xsim.log"
            state_file = bench_root / "bench.json"
            if state_file.is_file():
                try:
                    bench_state = json.loads(state_file.read_text(encoding="utf-8"))
                    bench_status = bench_state.get("status", "unknown")
                    if bench_status == "passed":
                        expected = bench_state.get("log_sha256") or bench_state.get("outputs", {}).get(log_name)
                        log = bench_root / log_name
                        status = "passed" if log.is_file() and sha256_file(log) == expected else "unknown"
                    else:
                        status = bench_status if bench_status in {"failed", "running"} else "unknown"
                except (OSError, ValueError):
                    status = "unknown"
        test_record = {
            "name": test["name"], "suite": test["suite"], "top": test["top"],
            "sources": list(test["sources"]), "defines": list(test["defines"]),
            "iverilog_sources": list(test["iverilog_sources"]),
            "iverilog": test["iverilog"],
            "success_markers": list(test["success_markers"]),
            "parallel": test["parallel"],
            "resources": list(test["resources"]),
            "artifacts": list(test["artifacts"]),
            "status": status, "stage": stage_name or "", "run_id": run_id or "",
        }
        if test["timeout_seconds"] is not None:
            test_record["timeout_seconds"] = test["timeout_seconds"]
        tests.append(test_record)
    return {
        "schema_version": 1,
        "project": {
            "id": project.project_id,
            "name": project.name,
            "profile": project.profile,
            "manifest": project.path.name,
            "output_dir": project.output_dir.relative_to(project.root).as_posix(),
            "tools": dict(project.tool_versions),
        },
        "sources": sources,
        "tests": tests,
        "dependencies": [
            {
                "name": name,
                "version": value["version"],
                "package_dir": value["package_dir"],
                "component_xml": value["component_xml"] or "",
                "metadata": list(value["metadata"]),
            }
            for name, value in sorted(project.dependencies.items())
        ],
    }


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def inventory_toml(data: Mapping[str, Any]) -> str:
    project = data["project"]
    lines = [
        f"schema_version = {int(data['schema_version'])}",
        "",
        "[project]",
        f"id = {_toml_string(project['id'])}",
        f"name = {_toml_string(project['name'])}",
        f"profile = {_toml_string(project['profile'])}",
        f"manifest = {_toml_string(project['manifest'])}",
        f"output_dir = {_toml_string(project['output_dir'])}",
    ]
    for key, value in sorted(project.get("tools", {}).items()):
        lines.append(f"{key} = {_toml_string(value)}")
    for source in data["sources"]:
        lines.extend([
            "",
            "[[sources]]",
            f"path = {_toml_string(source['path'])}",
            f"role = {_toml_string(source['role'])}",
            f"fileset = {_toml_string(source['fileset'])}",
            f"active = {'true' if source['active'] else 'false'}",
            f"used_in = {_toml_array(list(source['used_in']))}",
            f"sha256 = {_toml_string(source['sha256'])}",
            f"working_tree = {_toml_string(source['working_tree'])}",
        ])
    for test in data["tests"]:
        test_lines = [
            "",
            "[[tests]]",
            f"name = {_toml_string(test['name'])}",
            f"suite = {_toml_string(test['suite'])}",
            f"top = {_toml_string(test['top'])}",
            f"sources = {_toml_array(test['sources'])}",
            f"defines = {_toml_array(test['defines'])}",
            f"iverilog_sources = {_toml_array(test['iverilog_sources'])}",
            f"iverilog = {'true' if test['iverilog'] else 'false'}",
            f"success_markers = {_toml_array(test['success_markers'])}",
            f"parallel = {'true' if test['parallel'] else 'false'}",
            f"resources = {_toml_array(test['resources'])}",
            f"artifacts = {_toml_array(test['artifacts'])}",
            f"stage = {_toml_string(test['stage'])}",
            f"status = {_toml_string(test['status'])}",
            f"run_id = {_toml_string(test['run_id'])}",
        ]
        if "timeout_seconds" in test:
            test_lines.insert(9, f"timeout_seconds = {int(test['timeout_seconds'])}")
        lines.extend(test_lines)
    for dependency in data["dependencies"]:
        lines.extend([
            "",
            "[[dependencies]]",
            f"name = {_toml_string(dependency['name'])}",
            f"version = {_toml_string(dependency['version'])}",
            f"package_dir = {_toml_string(dependency['package_dir'])}",
            f"component_xml = {_toml_string(dependency['component_xml'])}",
            f"metadata = {_toml_array(dependency['metadata'])}",
        ])
    return "\n".join(lines) + "\n"
