"""发现 AMD 主机工具链并检查版本锁。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Sequence

from .runner import run_command


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
        return self.status == "ok"


def probe_toolchain(
    name: str,
    command: str,
    version_args: Sequence[str] = ("--version",),
    expected_version: str | None = None,
    *,
    cwd: Path,
    log_dir: Path | None = None,
) -> ToolchainResult:
    """解析可执行程序并查询版本，按需执行版本锁检查。"""
    resolved = shutil.which(command)
    if resolved is None and Path(command).is_absolute() and Path(command).is_file():
        resolved = str(Path(command).resolve())
    if resolved is None:
        return ToolchainResult(name, command, None, expected_version, None, "missing", f"未找到可执行程序：{command}")
    result = run_command([resolved, *version_args], cwd=cwd, timeout_seconds=30, log_dir=log_dir)
    reported = (result.stdout + "\n" + result.stderr).strip()
    if not result.succeeded:
        return ToolchainResult(name, command, resolved, expected_version, reported or None, "error",
                               result.error or f"版本查询命令退出码为 {result.exit_code}")
    if expected_version and expected_version not in reported:
        return ToolchainResult(name, command, resolved, expected_version, reported, "mismatch",
                               f"输出中未找到预期版本标记 {expected_version!r}")
    return ToolchainResult(name, command, resolved, expected_version, reported, "ok", "版本探测通过")
