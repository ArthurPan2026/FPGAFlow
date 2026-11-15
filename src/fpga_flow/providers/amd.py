"""AMD Zynq JTAG 与 QSPI 阶段提供器。

器件名称、板卡身份、产物选择器和 PS 初始化
过程名称均由消费项目的 profile 提供。
"""
from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path, PurePosixPath
import time
from typing import Any, Mapping, Sequence

from ..amd_deploy import (
    DeployCommandError,
    build_qspi_program_argv,
    parse_program_flash_targets,
    program_flash_query_argv,
    render_zynq_jtag_program_tcl,
    select_unique_flash_target,
)
from ..fingerprint import FingerprintError, hash_artifact
from ..models import FlowConfig, StageConfig
from ..runner import CommandResult, run_command


class AMDProviderError(ValueError):
    """profile 无法安全描述所选 AMD 板级操作时抛出。"""


def _target_options(stage: StageConfig, profile: FlowConfig, target: str | None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if target is not None:
        target_values = profile.targets.get(target)
        if isinstance(target_values, Mapping):
            values.update(target_values)
    values.update(dict(stage.options))
    return values


def _relative_artifact(value: Any, stage: StageConfig, profile: FlowConfig) -> tuple[Path, str]:
    if not isinstance(value, str) or not value.strip():
        raise AMDProviderError("产物选项必须是非空的项目相对路径或 glob 模式")
    pattern = value.strip()
    raw = PurePosixPath(pattern)
    if raw.is_absolute() or ".." in raw.parts or "\\" in pattern:
        raise AMDProviderError(f"产物路径超出项目目录：{pattern!r}")
    matches = sorted((p for p in profile.root.glob(pattern) if p.is_file() and not p.is_symlink()), key=lambda p: p.as_posix())
    if len(matches) != 1:
        raise AMDProviderError(f"产物选择器必须恰好匹配一个文件：{pattern!r}，实际匹配 {len(matches)} 个")
    path = matches[0].resolve(strict=True)
    try:
        relative = path.relative_to(profile.root.resolve(strict=True)).as_posix()
    except ValueError as exc:
        raise AMDProviderError(f"产物路径超出项目根目录：{pattern!r}") from exc
    declared_by_dependency = any(
        fnmatch.fnmatchcase(relative, output_pattern)
        for dependency_name in stage.needs
        for output_pattern in profile.stages[dependency_name].outputs
    )
    if stage.needs and not declared_by_dependency:
        raise AMDProviderError(f"前置阶段没有声明产物 {relative!r}")
    try:
        hash_artifact(profile.root, relative)
    except (FingerprintError, OSError, ValueError) as exc:
        raise AMDProviderError(f"产物不安全或无法读取：{relative}：{exc}") from exc
    return path, relative


def _toolchain_prefix(stage: StageConfig, profile: FlowConfig, name: str) -> list[str]:
    if name not in stage.toolchains:
        raise AMDProviderError(f"阶段必须锁定工具链 {name!r}")
    toolchain = profile.toolchains.get(name)
    if toolchain is None or not toolchain.command:
        raise AMDProviderError(f"工具链 {name!r} 未配置可执行命令")
    return list(toolchain.command)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ZynqJTAGProvider:
    """使用 XSCT 执行 PL+PS 联合部署，并由 profile 提供目标筛选条件。"""

    def execute(self, stage: StageConfig, profile: FlowConfig, run_dir: Path,
                target: str | None) -> CommandResult:
        options = _target_options(stage, profile, target)
        required = (
            "bitstream", "ps_init_script", "elf", "hw_server_url",
            "ps_target_pattern", "pl_target_pattern", "cpu_target_pattern", "ps_init_commands",
        )
        missing = [name for name in required if name not in options]
        if missing:
            raise AMDProviderError(f"JTAG profile 缺少选项：{', '.join(missing)}")
        bit, _ = _relative_artifact(options["bitstream"], stage, profile)
        ps_init, _ = _relative_artifact(options["ps_init_script"], stage, profile)
        elf, _ = _relative_artifact(options["elf"], stage, profile)
        init_commands = options["ps_init_commands"]
        if not isinstance(init_commands, (list, tuple)) or not all(isinstance(item, str) for item in init_commands):
            raise AMDProviderError("ps_init_commands 必须是过程名称数组")
        try:
            run_ms = int(options.get("run_ms", 1000))
        except (TypeError, ValueError) as exc:
            raise AMDProviderError("run_ms 必须是整数") from exc
        script = run_dir / "jtag_program.tcl"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(render_zynq_jtag_program_tcl(
            bit, ps_init, elf,
            hw_server_url=str(options["hw_server_url"]),
            run_ms=run_ms,
            ps_target_pattern=str(options["ps_target_pattern"]),
            pl_target_pattern=str(options["pl_target_pattern"]),
            cpu_target_pattern=str(options["cpu_target_pattern"]),
            ps_init_commands=init_commands,
        ), encoding="utf-8", newline="\n")
        command = [*_toolchain_prefix(stage, profile, "xsct"), str(script)]
        return run_command(command, cwd=profile.root, env=stage.env,
                           timeout_seconds=stage.timeout_seconds)


class QSPIProgramProvider:
    """对一个 QSPI 镜像进行编程，并执行空白检查和读回验证。"""

    def execute(self, stage: StageConfig, profile: FlowConfig, run_dir: Path,
                target: str | None) -> CommandResult:
        options = _target_options(stage, profile, target)
        required = ("boot_image", "fsbl", "flash_type", "offset", "target_device_name")
        missing = [name for name in required if name not in options]
        if missing:
            raise AMDProviderError(f"QSPI profile 缺少选项：{', '.join(missing)}")
        server_url = str(options.get("server_url", ""))
        boot_image, image_rel = _relative_artifact(options["boot_image"], stage, profile)
        fsbl, fsbl_rel = _relative_artifact(options["fsbl"], stage, profile)
        try:
            offset = int(options["offset"])
        except (TypeError, ValueError) as exc:
            raise AMDProviderError("QSPI 偏移量必须是整数") from exc
        flash_size = options.get("flash_size_bytes")
        if flash_size is not None and offset + boot_image.stat().st_size > int(flash_size):
            raise AMDProviderError("QSPI 镜像超出配置的 Flash 容量")
        prefix = _toolchain_prefix(stage, profile, "program_flash")
        query = [*prefix, "-jtagtargets", "-url", server_url]
        query_dir = run_dir / "target-query"
        query_result = run_command(query, cwd=profile.root, env=stage.env,
                                   timeout_seconds=min(stage.timeout_seconds or 90, 90),
                                   log_dir=query_dir)
        if not query_result.succeeded:
            return query_result
        targets = parse_program_flash_targets(query_result.stdout + "\n" + query_result.stderr)
        try:
            selected = select_unique_flash_target(
                targets,
                device_name=str(options["target_device_name"]),
                target_name_contains=options.get("target_name_contains"),
                idcode=options.get("target_idcode"),
            )
        except DeployCommandError as exc:
            selection = {"status": "failed", "error": str(exc), "targets": targets}
            (run_dir / "target-selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
            return CommandResult(tuple(query), profile.root, query_result.started_at_utc,
                                 _now(), query_result.elapsed_seconds, 2, False,
                                 query_result.stdout, f"{exc}\n", str(exc))
        selection = {"status": "selected", "target": dict(selected), "targets": targets,
                     "boot_image": image_rel, "fsbl": fsbl_rel,
                     "offset": offset, "flash_type": str(options["flash_type"]),
                     "verify": True, "blank_check": True, "erase_all": False}
        (run_dir / "target-selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
        qspi_argv = build_qspi_program_argv(
            prefix[0], boot_image, fsbl, flash_type=str(options["flash_type"]),
            offset=offset, target_name=str(selected["target_name"]), server_url=server_url,
        )
        command = [*prefix, *qspi_argv[1:]]
        program_result = run_command(command, cwd=profile.root, env=stage.env,
                                      timeout_seconds=stage.timeout_seconds,
                                      log_dir=run_dir / "program")
        output_text = program_result.stdout + "\n" + program_result.stderr
        verify_marker = str(options.get("verify_marker", "Verify Operation successful"))
        if program_result.succeeded and verify_marker not in output_text:
            return CommandResult(tuple(command), profile.root, query_result.started_at_utc,
                                 program_result.completed_at_utc,
                                 query_result.elapsed_seconds + program_result.elapsed_seconds,
                                 1, False, program_result.stdout,
                                 program_result.stderr + f"\n缺少必需的读回验证标记：{verify_marker}\n",
                                 f"缺少必需的读回验证标记：{verify_marker}")
        return CommandResult(tuple(command), profile.root, query_result.started_at_utc,
                             program_result.completed_at_utc,
                             query_result.elapsed_seconds + program_result.elapsed_seconds,
                             program_result.exit_code, program_result.timed_out,
                             program_result.stdout, program_result.stderr, program_result.error)


def builtin_providers() -> dict[str, Any]:
    """返回供 `FlowEngine` 构造时使用的内置 AMD 提供器注册表。"""
    from .uart import UARTCommandProvider

    return {
        "amd.zynq.jtag": ZynqJTAGProvider(),
        "amd.qspi.program": QSPIProgramProvider(),
        "amd.uart.commands": UARTCommandProvider(),
    }
