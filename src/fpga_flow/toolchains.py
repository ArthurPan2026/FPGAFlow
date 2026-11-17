"""发现 AMD 主机工具链并检查版本锁。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
from typing import Sequence

from .runner import run_command


_FATAL_OUTPUT = re.compile(r"^\s*(?:error|fatal)(?:\s*[:\[]|\s)", re.IGNORECASE)


@dataclass(frozen=True)
class ToolchainResult:
    name: str
    configured_command: str
    resolved_command: str | None
    expected_version: str | None
    reported_version: str | None
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "warning"}


def probe_toolchain(
    name: str,
    command: str,
    version_args: Sequence[str] = ("--version",),
    expected_version: str | None = None,
    *,
    cwd: Path,
    log_dir: Path | None = None,
    failure_markers: Sequence[str] = (),
) -> ToolchainResult:
    """解析可执行程序，检查退出状态、致命输出和版本锁。"""
    resolved = shutil.which(command)
    if resolved is None and Path(command).is_absolute() and Path(command).is_file():
        resolved = str(Path(command).resolve())
    if resolved is None:
        return ToolchainResult(name, command, None, expected_version, None, "missing",
                               f"未找到可执行程序：{command}")
    result = run_command([resolved, *version_args], cwd=cwd, timeout_seconds=30, log_dir=log_dir)
    reported = (result.stdout + "\n" + result.stderr).strip()
    if not result.succeeded:
        return ToolchainResult(name, command, resolved, expected_version, reported or None, "error",
                               result.error or f"版本查询命令退出码为 {result.exit_code}")
    fatal_lines = [line.strip() for line in reported.splitlines() if _FATAL_OUTPUT.match(line)]
    configured_failures = [marker for marker in failure_markers if marker and marker in reported]
    if configured_failures:
        return ToolchainResult(name, command, resolved, expected_version, reported, "error",
                               "版本查询输出包含配置的失败标记：" + "；".join(configured_failures[:5]))
    if fatal_lines:
        display_only = all("cannot open display" in line.lower() for line in fatal_lines)
        if display_only and (not expected_version or expected_version in reported):
            return ToolchainResult(name, command, resolved, expected_version, reported, "warning",
                                   "版本已识别，但出现 Cannot open DISPLAY；阶段命令仍需自行验证")
        return ToolchainResult(name, command, resolved, expected_version, reported, "error",
                               "版本查询输出包含错误信息：" + "；".join(fatal_lines[:5]))
    if expected_version and expected_version not in reported:
        return ToolchainResult(name, command, resolved, expected_version, reported, "mismatch",
                               f"输出中未找到预期版本标记 {expected_version!r}")
    return ToolchainResult(name, command, resolved, expected_version, reported, "ok", "版本探测通过")
