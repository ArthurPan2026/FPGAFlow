"""Cross-project FPGAFlow workspace DAG and verified release import."""
from __future__ import annotations
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, tomllib, uuid, zipfile
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET
from typing import Any, Callable, Mapping
from .locking import ProjectRunLock, RunLockError
from .project import ProjectDescriptor, ProjectError, load_project

SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_NODE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")

class WorkspaceError(ValueError):
    """Invalid or failed workspace operation."""

@dataclass(frozen=True)
class WorkspaceProject:
    name: str
    manifest: Path
    descriptor: ProjectDescriptor

@dataclass(frozen=True)
class WorkspaceNode:
    name: str
    kind: str
    needs: tuple[str, ...]
    resources: tuple[str, ...]
    raw: Mapping[str, Any]

@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    root: Path
    workspace_id: str
    output_dir: Path
    resource_lock_dir: Path
    projects: Mapping[str, WorkspaceProject]
    nodes: Mapping[str, WorkspaceNode]
    order: tuple[str, ...]

def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def tree_hash(path: Path) -> str:
    if path.is_symlink():
        raise WorkspaceError(f"refusing symlink: {path}")
    if path.is_file():
        return _sha_file(path)
    if not path.is_dir():
        raise WorkspaceError(f"tree does not exist: {path}")
    entries = {}
    for item in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix()):
        if item.is_symlink():
            raise WorkspaceError(f"refusing symlink in tree: {item}")
        if item.is_file():
            entries[item.relative_to(path).as_posix()] = _sha_file(item)
    return _sha_bytes(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())

def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise WorkspaceError(f"refusing symlink in workspace state: {path}")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def _relative(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"{where} must be a relative path")
    text = value.strip().replace("\\", "/")
    rel = PurePosixPath(text)
    if rel.is_absolute() or ".." in rel.parts or "\x00" in text:
        raise WorkspaceError(f"{where} must stay inside its project: {value!r}")
    target = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"{where} contains a symlink: {current}")
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise WorkspaceError(f"{where} escapes its root: {target}") from exc
    return target

def _order(nodes: Mapping[str, WorkspaceNode]) -> tuple[str, ...]:
    marks, result, chain = {}, [], []
    def visit(name: str) -> None:
        mark = marks.get(name, 0)
        if mark == 2:
            return
        if mark == 1:
            pos = chain.index(name) if name in chain else 0
            raise WorkspaceError("dependency cycle: " + " -> ".join(chain[pos:] + [name]))
        marks[name] = 1
        chain.append(name)
        for dep in nodes[name].needs:
            if dep not in nodes:
                raise WorkspaceError(f"node {name!r} needs unknown node {dep!r}")
            visit(dep)
        chain.pop()
        marks[name] = 2
        result.append(name)
    for name in nodes:
        visit(name)
    return tuple(result)

def load_workspace(path: str | Path) -> WorkspaceConfig:
    original = Path(path).expanduser()
    if original.is_symlink():
        raise WorkspaceError(f"workspace manifest is a symlink: {original}")
    manifest = original.resolve(strict=True)
    if not manifest.is_file():
        raise WorkspaceError(f"workspace manifest is not a file: {manifest}")
    try:
        raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceError(f"cannot parse workspace {manifest}: {exc}") from exc
    if set(raw) != {"schema_version", "workspace", "projects", "nodes"}:
        raise WorkspaceError("workspace requires schema_version, workspace, projects and nodes")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise WorkspaceError(f"workspace schema_version must be {SCHEMA_VERSION}")
    root = manifest.parent.resolve(strict=True)
    meta = raw["workspace"]
    if not isinstance(meta, dict) or set(meta) - {"id", "output_dir", "resource_lock_dir"}:
        raise WorkspaceError("invalid [workspace] keys")
    workspace_id = meta.get("id")
    if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id):
        raise WorkspaceError("workspace.id is invalid")
    output = _relative(root, meta.get("output_dir", "build/fpga-flow/workspace"), "workspace.output_dir")
    if meta.get("resource_lock_dir"):
        locks = Path(meta["resource_lock_dir"]).expanduser()
        if not locks.is_absolute():
            raise WorkspaceError("workspace.resource_lock_dir must be absolute")
        locks = locks.resolve(strict=False)
    else:
        base = os.environ.get("FPGA_FLOW_STATE_ROOT")
        if base:
            locks = Path(base).expanduser() / "workspace-resources"
        elif os.name == "nt":
            locks = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "FPGAFlow/workspace-resources"
        else:
            locks = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "fpga-flow/workspace-resources"
    projects_raw = raw["projects"]
    if not isinstance(projects_raw, dict) or not projects_raw:
        raise WorkspaceError("workspace.projects must be a non-empty table")
    projects = {}
    for name, item in projects_raw.items():
        if not isinstance(name, str) or not _ID.fullmatch(name) or not isinstance(item, dict) or set(item) != {"manifest"}:
            raise WorkspaceError(f"invalid project reference {name!r}")
        value = item["manifest"]
        if not isinstance(value, str) or not value:
            raise WorkspaceError(f"projects.{name}.manifest must be a path")
        candidate = root / value
        if candidate.is_symlink():
            raise WorkspaceError(f"project manifest is a symlink: {candidate}")
        project_manifest = candidate.resolve(strict=True)
        try:
            descriptor = load_project(project_manifest)
        except ProjectError as exc:
            raise WorkspaceError(f"invalid project {name!r}: {exc}") from exc
        projects[name] = WorkspaceProject(name, project_manifest, descriptor)
    nodes_raw = raw["nodes"]
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise WorkspaceError("workspace.nodes must be a non-empty array")
    nodes = {}
    base_keys = {"name", "kind", "needs", "resources"}
    flow_keys = base_keys | {"project", "action", "stage", "suite", "target", "side_effects", "destructive", "optional"}
    import_keys = base_keys | {"source_project", "source_node", "target_project", "release_stage",
        "expected_version", "expected_vlnv", "expected_core_revision", "ip_asset", "ps_asset",
        "ip_prefix", "ps_prefix", "ip_target", "ps_target", "dependency_name", "dependency_lock_file"}
    for index, item in enumerate(nodes_raw):
        if not isinstance(item, dict):
            raise WorkspaceError(f"nodes[{index}] must be a table")
        name, kind = item.get("name"), item.get("kind")
        if not isinstance(name, str) or not _NODE.fullmatch(name) or name in nodes:
            raise WorkspaceError(f"nodes[{index}] has an invalid or duplicated name")
        if kind not in {"flow", "artifact_import"}:
            raise WorkspaceError(f"node {name!r} kind must be flow or artifact_import")
        if set(item) - (flow_keys if kind == "flow" else import_keys):
            raise WorkspaceError(f"node {name!r} has unsupported keys")
        needs, resources = item.get("needs", []), item.get("resources", [])
        if not isinstance(needs, list) or any(not isinstance(v, str) for v in needs) or len(needs) != len(set(needs)):
            raise WorkspaceError(f"node {name!r}.needs must be a unique string array")
        if not isinstance(resources, list) or any(not isinstance(v, str) or not v for v in resources) or len(resources) != len(set(resources)):
            raise WorkspaceError(f"node {name!r}.resources must be a unique string array")
        if kind == "flow":
            if item.get("project") not in projects or item.get("action") not in {"validate", "build", "release", "deploy"}:
                raise WorkspaceError(f"node {name!r} has an invalid project/action")
            if item["action"] == "validate" and not isinstance(item.get("suite"), str):
                raise WorkspaceError(f"node {name!r} validate action needs suite")
            if item["action"] == "build" and not isinstance(item.get("stage"), str):
                raise WorkspaceError(f"node {name!r} build action needs stage")
            if item["action"] == "deploy" and not all(isinstance(item.get(k), str) for k in ("stage", "target")):
                raise WorkspaceError(f"node {name!r} deploy action needs stage and target")
            for flag in ("side_effects", "destructive", "optional"):
                if flag in item and type(item[flag]) is not bool:
                    raise WorkspaceError(f"node {name!r}.{flag} must be boolean")
        else:
            required = ("source_project", "source_node", "target_project", "expected_vlnv",
                "ip_asset", "ps_asset", "ip_prefix", "ps_prefix", "ip_target", "ps_target",
                "dependency_name", "dependency_lock_file")
            if any(not isinstance(item.get(k), str) or not item[k] for k in required):
                raise WorkspaceError(f"node {name!r} is missing import settings")
            if item["source_project"] not in projects or item["target_project"] not in projects:
                raise WorkspaceError(f"node {name!r} references an unknown project")
            if "expected_core_revision" in item and (type(item["expected_core_revision"]) is not int or item["expected_core_revision"] < 0):
                raise WorkspaceError(f"node {name!r}.expected_core_revision must be a non-negative integer")
        nodes[name] = WorkspaceNode(name, kind, tuple(needs), tuple(resources), dict(item))
    return WorkspaceConfig(manifest, root, workspace_id, output, locks, projects, nodes, _order(nodes))

def _safe_member(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise WorkspaceError(f"unsafe archive path {name!r}")
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise WorkspaceError(f"unsafe archive path {name!r}")
    if re.match(r"^[A-Za-z]:", member.parts[0]):
        raise WorkspaceError(f"drive-qualified archive path {name!r}")
    return member

def _zip_infos(path: Path) -> tuple[zipfile.ZipInfo, ...]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = tuple(archive.infolist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise WorkspaceError(f"invalid release archive {path}: {exc}") from exc
    seen = set()
    for info in infos:
        key = _safe_member(info.filename).as_posix().rstrip("/")
        if key in seen:
            raise WorkspaceError(f"duplicate ZIP member {key!r}")
        seen.add(key)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise WorkspaceError(f"symlink ZIP member is forbidden: {info.filename}")
    return infos

def _zip_bytes(path: Path, member: str) -> bytes:
    _zip_infos(path)
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read(member)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise WorkspaceError(f"missing or unreadable {member!r} in {path.name}") from exc

def _extract_prefix(archive_path: Path, prefix: str, destination: Path) -> int:
    prefix = _safe_member(prefix.rstrip("/") + "/").as_posix().rstrip("/") + "/"
    count = 0
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for info in _zip_infos(archive_path):
            member = _safe_member(info.filename).as_posix()
            if member == prefix.rstrip("/") or not member.startswith(prefix):
                continue
            remainder = member[len(prefix):]
            if not remainder:
                continue
            relative = _safe_member(remainder)
            target = destination.joinpath(*relative.parts)
            target.resolve(strict=False).relative_to(destination.resolve(strict=True))
            if info.is_dir() or info.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as out:
                    shutil.copyfileobj(source, out)
                count += 1
    if not count:
        raise WorkspaceError(f"archive {archive_path.name} has no files under {prefix!r}")
    return count

def _safe_target(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise WorkspaceError(f"unsafe consumer target {relative!r}")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceError(f"refusing symlink in consumer target: {current}")
    current.resolve(strict=False).relative_to(root.resolve(strict=True))
    return current

def _backup_tree(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise WorkspaceError(f"cannot snapshot existing target {source}")
    if tree_hash(source) != tree_hash(destination):
        raise WorkspaceError(f"backup verification failed for {source}")
    return tree_hash(destination)

def _find_release(config: WorkspaceConfig, node: WorkspaceNode, results: Mapping[str, Any]) -> dict[str, Any]:
    raw = node.raw
    source_node = str(raw["source_node"])
    source_result = results.get(source_node)
    if not isinstance(source_result, Mapping):
        raise WorkspaceError(f"source node {source_node!r} has no result")
    flow = source_result.get("flow", {})
    stages = flow.get("stages", []) if isinstance(flow, Mapping) else []
    stage_name = str(raw.get("release_stage", "release"))
    matches = [x for x in stages if isinstance(x, Mapping) and x.get("stage") == stage_name]
    if len(matches) != 1 or matches[0].get("status") != "succeeded":
        raise WorkspaceError(f"source node has no successful {stage_name!r} stage")
    stage = matches[0]
    source_project = config.projects[str(raw["source_project"])]
    canonical_version = source_project.descriptor.package_version
    if not isinstance(canonical_version, str) or not canonical_version:
        raise WorkspaceError("canonical project has no package VERSION")
    pinned_version = raw.get("expected_version")
    if pinned_version is not None and pinned_version != canonical_version:
        raise WorkspaceError("workspace expected_version differs from canonical VERSION")
    def release_asset(key: str) -> str:
        template = str(raw[key])
        name = template.replace("{package_version}", canonical_version)
        if "{" in name or "}" in name:
            raise WorkspaceError(f"unsupported release filename token in {template!r}")
        return name
    output_root = stage.get("output_root")
    if not isinstance(output_root, str):
        raise WorkspaceError("release stage is missing output_root")
    physical_root = (source_project.descriptor.root / output_root).resolve(strict=True)
    physical_root.relative_to(source_project.descriptor.root)
    outputs = stage.get("outputs")
    if not isinstance(outputs, Mapping):
        raise WorkspaceError("release stage is missing output hashes")
    def get_output(filename: str) -> tuple[Path, str]:
        hits = [(rel, digest) for rel, digest in outputs.items()
                if isinstance(rel, str) and Path(rel).name == filename and isinstance(digest, str)]
        if len(hits) != 1:
            raise WorkspaceError(f"release outputs need exactly one {filename!r}")
        rel, digest = hits[0]
        path = (source_project.descriptor.root / rel).resolve(strict=True)
        path.relative_to(physical_root)
        if _sha_file(path) != digest:
            raise WorkspaceError(f"release artifact differs from Flow output hash: {filename}")
        return path, digest
    ip, ip_hash = get_output(release_asset("ip_asset"))
    ps, ps_hash = get_output(release_asset("ps_asset"))
    sums, sums_hash = get_output("SHA256SUMS")
    sums_map = {}
    for line in sums.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            raise WorkspaceError(f"malformed SHA256SUMS line: {line!r}")
        sums_map[fields[1]] = fields[0].lower()
    if sums_map.get(ip.name) != ip_hash.lower() or sums_map.get(ps.name) != ps_hash.lower():
        raise WorkspaceError("SHA256SUMS does not match the IP and PS archives")
    ip_prefix = str(raw["ip_prefix"]).rstrip("/")
    ip_meta = json.loads(_zip_bytes(ip, f"{ip_prefix}/RELEASE-METADATA.json"))
    ps_meta = json.loads(_zip_bytes(ps, "embedded-video-ip-ps-zynq7000/RELEASE-METADATA.json"))
    version, vlnv = canonical_version, str(raw["expected_vlnv"])
    if ip_meta.get("version") != version or ps_meta.get("version") != version:
        raise WorkspaceError("IP and PS versions do not match expected_version")
    if ip_meta.get("vlnv") != vlnv:
        raise WorkspaceError(f"unexpected IP VLNV: {ip_meta.get('vlnv')!r}")
    if ip_meta.get("source_commit") != ps_meta.get("source_commit"):
        raise WorkspaceError("IP and PS archives have different source commits")
    component = ET.fromstring(_zip_bytes(ip, f"{ip_prefix}/ip_repo/panfpga/embedded_video_ip/component.xml"))
    revision = next((e.text for e in component.iter() if e.tag.endswith("}coreRevision")), None)
    metadata_revision = ip_meta.get("core_revision")
    if revision is None or not revision.isdigit() or type(metadata_revision) is not int or metadata_revision != int(revision):
        raise WorkspaceError("release metadata core_revision differs from packaged component.xml")
    expected_revision = raw.get("expected_core_revision")
    if expected_revision is not None and revision != str(expected_revision):
        raise WorkspaceError(f"coreRevision mismatch: expected {expected_revision}, got {revision!r}")
    if _zip_bytes(ip, f"{ip_prefix}/VERSION").decode("utf-8").strip() != version:
        raise WorkspaceError("IP VERSION file differs from release metadata")
    if not ip_meta.get("canonical_source_sha256"):
        raise WorkspaceError("release metadata has no canonical source hash")
    _zip_infos(ip)
    _zip_infos(ps)
    return {"release_stage_run_id": stage.get("run_id"),
        "ip": {"path": ip, "filename": ip.name, "sha256": ip_hash, "metadata": ip_meta, "prefix": ip_prefix},
        "ps": {"path": ps, "filename": ps.name, "sha256": ps_hash, "metadata": ps_meta,
               "prefix": str(raw["ps_prefix"]).rstrip("/")},
        "sha256sums": {"path": sums.as_posix(), "sha256": sums_hash},
        "version": version, "vlnv": vlnv, "core_revision": revision}

class WorkspaceDAG:
    def __init__(self, config: WorkspaceConfig,
                 flow_runner: Callable[[WorkspaceProject, WorkspaceNode, Path, bool, bool, bool], Mapping[str, Any]] | None = None):
        self.config = config
        self.flow_runner = flow_runner or self._flow

    @classmethod
    def from_file(cls, path: str | Path, flow_runner: Callable[..., Mapping[str, Any]] | None = None) -> "WorkspaceDAG":
        return cls(load_workspace(path), flow_runner=flow_runner)

    def _fingerprints(self) -> dict[str, Any]:
        return {"workspace_manifest_sha256": _sha_file(self.config.path),
            "projects": {name: {"root": p.descriptor.root.as_posix(), "manifest": p.manifest.as_posix(),
                "manifest_sha256": _sha_file(p.manifest), "project_id": p.descriptor.project_id}
                for name, p in self.config.projects.items()}}

    def plan(self) -> dict[str, Any]:
        nodes = []
        for name in self.config.order:
            node = self.config.nodes[name]
            item = {"node": name, "kind": node.kind, "needs": list(node.needs), "resources": list(node.resources)}
            if node.kind == "flow":
                item.update({key: node.raw.get(key) for key in ("project", "action", "stage", "suite", "target")})
                item["optional"] = bool(node.raw.get("optional"))
                item["default_action"] = "skip" if node.raw.get("optional") else "run"
                item["requires_side_effect_ack"] = bool(node.raw.get("side_effects") or node.raw.get("destructive"))
            else:
                item.update({key: node.raw.get(key) for key in ("source_project", "source_node", "target_project",
                    "expected_version", "expected_vlnv")})
            nodes.append(item)
        return {"status": "ready", "workspace": self.config.workspace_id, "manifest": str(self.config.path),
            "output_dir": str(self.config.output_dir), "fingerprints": self._fingerprints(),
            "nodes": nodes, "commands_launched": False}

    def _cli(self) -> list[str]:
        arg0 = Path(sys.argv[0])
        return [str(arg0.resolve())] if arg0.name in {"fpga-flow", "fpga-flow.exe"} else [sys.executable, "-m", "fpga_flow.cli"]

    def _flow(self, project: WorkspaceProject, node: WorkspaceNode, run_dir: Path,
              force: bool, allow_side_effects: bool, allow_destructive: bool) -> Mapping[str, Any]:
        raw = node.raw
        argv = [*self._cli(), "--project", str(project.manifest), "--json", "--details"]
        action = raw["action"]
        if action == "validate":
            argv += ["validate", "--suite", str(raw["suite"])]
            if force: argv.append("--force")
        elif action == "build":
            argv += ["build", "--stage", str(raw["stage"]), "--with-deps"]
            if force: argv.append("--force")
        elif action == "release":
            argv += ["release"]
            if force: argv.append("--force")
        else:
            argv += ["deploy", "--target", str(raw["target"]), "--stage", str(raw["stage"])]
            if allow_destructive and raw.get("destructive"): argv.append("--allow-destructive")
        if raw.get("target") and action != "deploy": argv += ["--target", str(raw["target"])]
        if allow_side_effects and raw.get("side_effects"): argv.append("--allow-side-effects")
        env = os.environ.copy()
        src = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        logs = run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out, err = logs / f"{node.name}.stdout.log", logs / f"{node.name}.stderr.log"
        (logs / f"{node.name}.command.json").write_text(json.dumps(argv, indent=2) + "\n", encoding="utf-8")
        with out.open("wb") as stdout, err.open("wb") as stderr:
            proc = subprocess.run(argv, cwd=project.descriptor.root, env=env, stdout=stdout, stderr=stderr, check=False)
        text = out.read_text(encoding="utf-8", errors="replace")
        if proc.returncode:
            tail = "\n".join((text + "\n" + err.read_text(encoding="utf-8", errors="replace")).splitlines()[-80:])
            raise WorkspaceError(f"node {node.name!r} exited {proc.returncode}:\n{tail}")
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"node {node.name!r} returned invalid JSON: {exc}") from exc
        if result.get("status") != "succeeded":
            raise WorkspaceError(f"node {node.name!r} returned {result.get('status')!r}")
        return {"command": argv, "exit_code": 0, "flow": result,
            "stdout_sha256": _sha_file(out), "stderr_sha256": _sha_file(err),
            "stdout_log": out.relative_to(run_dir).as_posix(), "stderr_log": err.relative_to(run_dir).as_posix()}

    def _locks(self, node: WorkspaceNode, run_id: str, wait_lock: bool) -> ExitStack:
        stack = ExitStack()
        try:
            self.config.resource_lock_dir.mkdir(parents=True, exist_ok=True)
            for resource in sorted(node.resources):
                key = _sha_bytes(resource.encode())[:24]
                lock = ProjectRunLock(self.config.resource_lock_dir / f"{key}.lock",
                    action=f"workspace:{self.config.workspace_id}:{resource}", command=run_id, wait=wait_lock)
                stack.enter_context(lock)
                lock.set_stage(node.name, run_id)
            return stack
        except Exception:
            stack.close()
            raise

    def _import(self, node: WorkspaceNode, results: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
        raw = node.raw
        release = _find_release(self.config, node, results)
        version = str(release["version"])
        project = self.config.projects[str(raw["target_project"])]
        root = project.descriptor.root
        specs = ((release["ip"], "ip_prefix", "ip_target"), (release["ps"], "ps_prefix", "ps_target"))
        installs = []
        for i, (archive, prefix_key, target_key) in enumerate(specs):
            target = _safe_target(root, str(raw[target_key]))
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".fpga-flow-import-{i}-", dir=target.parent))
            staging.rmdir()
            count = _extract_prefix(Path(archive["path"]), str(raw[prefix_key]), staging)
            prev = tree_hash(target) if target.exists() else None
            backup = run_dir / "backups" / f"{node.name}-{i}-{target.name}"
            backup_hash = _backup_tree(target, backup) if target.exists() else None
            installs.append({"target": target, "staging": staging, "previous": prev,
                "backup": backup, "backup_hash": backup_hash, "count": count, "sibling": None})
        lock_target = _safe_target(root, str(raw["dependency_lock_file"]))
        lock_target.parent.mkdir(parents=True, exist_ok=True)
        lock_data = {"schema_version": 1, "dependency": raw["dependency_name"],
            "version": release["version"], "vlnv": release["vlnv"],
            "core_revision": int(release["core_revision"]),
            "source_commit": release["ip"]["metadata"].get("source_commit"),
            "canonical_source_sha256": release["ip"]["metadata"].get("canonical_source_sha256"),
            "vivado_version": release["ip"]["metadata"].get("vivado_version"),
            "ip_archive": {"filename": release["ip"]["filename"], "sha256": release["ip"]["sha256"]},
            "ps_archive": {"filename": release["ps"]["filename"], "sha256": release["ps"]["sha256"]},
            "sha256sums_sha256": release["sha256sums"]["sha256"],
            "imported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        installed_trees = {str(raw["ip_target"]): tree_hash(installs[0]["staging"]),
                           str(raw["ps_target"]): tree_hash(installs[1]["staging"])}
        lock_data["installed_trees"] = installed_trees
        if lock_target.is_file():
            try:
                previous_lock = json.loads(lock_target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous_lock = None
            if isinstance(previous_lock, dict) and isinstance(previous_lock.get("imported_at_utc"), str):
                previous_identity = {key: value for key, value in previous_lock.items()
                                     if key != "imported_at_utc"}
                next_identity = {key: value for key, value in lock_data.items()
                                 if key != "imported_at_utc"}
                if previous_identity == next_identity:
                    # Import timestamps are audit metadata, not build inputs. Keep an
                    # identical dependency lock stable; the workspace run records each
                    # revalidation event separately.
                    lock_data["imported_at_utc"] = previous_lock["imported_at_utc"]
        lock_stage = lock_target.parent / f".{lock_target.name}.{uuid.uuid4().hex}.tmp"
        lock_stage.write_text(json.dumps(lock_data, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prev = tree_hash(lock_target) if lock_target.exists() else None
        backup = run_dir / "backups" / f"{node.name}-dependency-lock.json"
        backup_hash = _backup_tree(lock_target, backup) if lock_target.exists() else None
        installs.append({"target": lock_target, "staging": lock_stage, "previous": prev,
            "backup": backup, "backup_hash": backup_hash, "count": 1, "sibling": None})

        # Keep project dependency metadata in step with the release version.
        lock_relative = PurePosixPath(str(raw["dependency_lock_file"])).as_posix()
        dependency_names = [name for name, dep in project.descriptor.dependencies.items()
                            if dep.get("provenance", {}).get("lock_file") == lock_relative]
        if not dependency_names and str(raw["dependency_name"]) in project.descriptor.dependencies:
            dependency_names = [str(raw["dependency_name"])]
        if dependency_names:
            manifest_text = project.manifest.read_text(encoding="utf-8")
            for dependency in dependency_names:
                table = re.compile(rf"(?m)^\[dependencies\.{re.escape(dependency)}\][ \t]*$")
                match = table.search(manifest_text)
                if match is None:
                    raise WorkspaceError(f"target project is missing dependency table {dependency!r}")
                next_table = re.search(r"(?m)^\[", manifest_text[match.end():])
                stop = match.end() + next_table.start() if next_table else len(manifest_text)
                section = manifest_text[match.end():stop]
                version_line = re.compile(r'(?m)^(version\s*=\s*)"[^"\n]*"(.*)$')
                updated, count = version_line.subn(lambda item: item.group(1) + json.dumps(version) + item.group(2), section, count=1)
                if count != 1:
                    raise WorkspaceError(f"dependency table {dependency!r} has no single version field")
                manifest_text = manifest_text[:match.end()] + updated + manifest_text[stop:]
            manifest_target = project.manifest
            manifest_stage = manifest_target.parent / f".{manifest_target.name}.{uuid.uuid4().hex}.tmp"
            manifest_stage.write_text(manifest_text, encoding="utf-8", newline="\n")
            manifest_previous = tree_hash(manifest_target)
            manifest_backup = run_dir / "backups" / f"{node.name}-fpga-project.toml"
            manifest_backup_hash = _backup_tree(manifest_target, manifest_backup)
            installs.append({"target": manifest_target, "staging": manifest_stage,
                "previous": manifest_previous, "backup": manifest_backup,
                "backup_hash": manifest_backup_hash, "count": 1, "sibling": None})
        committed = []
        try:
            for item in installs:
                target = item["target"]
                if target.exists():
                    sibling = target.parent / f".{target.name}.fpga-flow-old-{uuid.uuid4().hex}"
                    os.replace(target, sibling)
                    item["sibling"] = sibling
                os.replace(item["staging"], target)
                committed.append(item)
        except Exception as exc:
            for item in reversed(installs):
                target = item["target"]
                if item in committed and target.exists():
                    shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
                sibling = item["sibling"]
                if sibling and sibling.exists(): os.replace(sibling, target)
            raise WorkspaceError(f"artifact import transaction rolled back: {exc}") from exc
        finally:
            for item in installs:
                stage = item["staging"]
                if stage.exists():
                    shutil.rmtree(stage) if stage.is_dir() and not stage.is_symlink() else stage.unlink(missing_ok=True)
        for item in installs:
            sibling = item["sibling"]
            if sibling and sibling.exists():
                shutil.rmtree(sibling) if sibling.is_dir() and not sibling.is_symlink() else sibling.unlink()
            item["installed"] = tree_hash(item["target"])
        return {"source_node": raw["source_node"], "release": {k: v for k, v in release.items() if k not in {"ip", "ps"}},
            "archives": {k: {"filename": release[k]["filename"], "sha256": release[k]["sha256"]} for k in ("ip", "ps")},
            "installations": [{"target": item["target"].relative_to(root).as_posix(),
                "previous_tree_sha256": item["previous"], "backup_tree_sha256": item["backup_hash"],
                "backup_path": item["backup"].relative_to(run_dir).as_posix() if item["backup_hash"] else None,
                "imported_tree_sha256": item["installed"], "file_count": item["count"]} for item in installs],
            "dependency_lock": lock_data}

    def run(self, *, force: bool = False, include_optional: bool = False,
            allow_side_effects: bool = False, allow_destructive: bool = False,
            wait_lock: bool = False) -> dict[str, Any]:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        outer = ProjectRunLock(self.config.output_dir / "workspace.lock", action="workspace-run", wait=wait_lock)
        try: outer.__enter__()
        except RunLockError as exc: raise WorkspaceError(str(exc)) from exc
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10]
        run_dir = self.config.output_dir / "runs" / run_id
        if run_dir.exists() or run_dir.is_symlink():
            outer.__exit__(None, None, None)
            raise WorkspaceError(f"workspace run directory already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        outer.set_run(run_id)
        record = {"schema_version": 1, "workspace": self.config.workspace_id, "run_id": run_id,
            "status": "running", "started_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "fingerprints": self._fingerprints(), "dependency_edges": [
                {"node": n, "needs": list(self.config.nodes[n].needs)} for n in self.config.order],
            "nodes": {}, "run_dir": run_dir.relative_to(self.config.root).as_posix()}
        results = {}
        _atomic_json(run_dir / "run.json", record)
        _atomic_json(self.config.output_dir / "latest-run.json", {"run_id": run_id, "status": "running"})
        try:
            for name in self.config.order:
                node = self.config.nodes[name]
                blocked = [dep for dep in node.needs
                           if record["nodes"].get(dep, {}).get("status") not in {"succeeded", "skipped"}]
                if blocked:
                    record["nodes"][name] = {"status": "blocked", "blocked_by": blocked}
                    _atomic_json(run_dir / "run.json", record)
                    continue
                if node.raw.get("optional") and not include_optional:
                    record["nodes"][name] = {"status": "skipped", "kind": node.kind,
                        "reason": "optional node; pass --include-optional to run it",
                        "needs": list(node.needs), "resources": list(node.resources)}
                    results[name] = {"skipped": True}
                    _atomic_json(run_dir / "run.json", record)
                    continue
                if node.kind == "flow" and node.raw.get("side_effects") and not allow_side_effects:
                    raise WorkspaceError(f"node {name!r} requires --allow-side-effects")
                if node.kind == "flow" and node.raw.get("destructive") and not allow_destructive:
                    raise WorkspaceError(f"node {name!r} requires --allow-destructive")
                outer.set_stage(name, run_id)
                item = {"status": "running", "kind": node.kind, "needs": list(node.needs),
                    "resources": list(node.resources),
                    "started_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
                record["nodes"][name] = item
                _atomic_json(run_dir / "run.json", record)
                print(f"[FPGA-FLOW-WORKSPACE] {name} started", file=sys.stderr, flush=True)
                try:
                    with self._locks(node, run_id, wait_lock):
                        result = (self.flow_runner(self.config.projects[str(node.raw["project"])], node, run_dir,
                                                   force, allow_side_effects, allow_destructive)
                                  if node.kind == "flow" else self._import(node, results, run_dir))
                    results[name] = result
                    item.update(dict(result))
                    item["status"] = "succeeded"
                    item["completed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    print(f"[FPGA-FLOW-WORKSPACE] {name} succeeded", file=sys.stderr, flush=True)
                except Exception as exc:
                    item["status"] = "failed"
                    item["error"] = str(exc)
                    item["completed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                finally:
                    outer.clear_stage(run_id)
                record["nodes"][name] = item
                _atomic_json(run_dir / "run.json", record)
            failed = [n for n, item in record["nodes"].items() if item.get("status") in {"failed", "blocked"}]
            record["status"] = "failed" if failed else "succeeded"
            record["failed_nodes"] = failed
            record["completed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _atomic_json(run_dir / "run.json", record)
            _atomic_json(self.config.output_dir / "latest-run.json", {"run_id": run_id, "status": record["status"]})
            if failed:
                raise WorkspaceError(f"workspace run failed or blocked at {', '.join(failed)}")
            return record
        except Exception as exc:
            if record.get("status") == "running":
                record["status"] = "failed"
                record["error"] = str(exc)
                record["completed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _atomic_json(run_dir / "run.json", record)
                _atomic_json(self.config.output_dir / "latest-run.json", {"run_id": run_id, "status": "failed"})
            raise
        finally:
            outer.__exit__(None, None, None)

    def status(self) -> dict[str, Any]:
        latest = self.config.output_dir / "latest-run.json"
        if not latest.is_file():
            return {"status": "empty", "workspace": self.config.workspace_id, "latest_run": None}
        try:
            info = json.loads(latest.read_text(encoding="utf-8"))
            record = json.loads((self.config.output_dir / "runs" / str(info["run_id"]) / "run.json").read_text(encoding="utf-8"))
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"cannot read latest workspace run: {exc}") from exc
        return {"status": record.get("status"), "workspace": self.config.workspace_id,
            "latest_run_id": record.get("run_id"), "run": record}

    def history(self, limit: int = 20) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise WorkspaceError("history limit must be 1..500")
        runs = []
        root = self.config.output_dir / "runs"
        if root.is_dir():
            for path in sorted(root.glob("*/run.json"), reverse=True):
                try: record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError): continue
                runs.append({k: record.get(k) for k in ("run_id", "status", "started_at_utc", "completed_at_utc", "failed_nodes")})
                if len(runs) >= limit: break
        return {"status": "succeeded", "workspace": self.config.workspace_id, "runs": runs, "limit": limit}
