"""无 shell 命令执行，并为每次运行保存持久证据。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Mapping, Sequence


_SECRET_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "PRIVATE", "API_KEY", "ACCESS_KEY")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _redacted_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {
        key: "<redacted>" if any(word in key.upper() for word in _SECRET_WORDS) else value
        for key, value in sorted(environment.items())
    }


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    cwd: Path
    started_at_utc: str
    completed_at_utc: str
    elapsed_seconds: float
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    log_dir: Path | None = None,
) -> CommandResult:
    """直接执行 argv，不通过 shell，并保存可检查的日志。"""
    argv = tuple(str(item) for item in command)
    if not argv or not argv[0]:
        raise ValueError("command 的 argv 必须包含非空可执行文件")
    working_directory = cwd.resolve()
    if not working_directory.is_dir():
        raise ValueError(f"工作目录不存在：{working_directory}")
    child_env = os.environ.copy()
    configured_env = {str(key): str(value) for key, value in (env or {}).items()}
    child_env.update(configured_env)
    started_text = utc_now()
    start = time.monotonic()
    timed_out = False
    error = None
    exit_code = 0
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            list(argv),
            cwd=working_directory,
            env=child_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = _to_text(exc.stdout)
        stderr = _to_text(exc.stderr)
        error = f"命令运行超过超时限制 {timeout_seconds} 秒"
    except OSError as exc:
        exit_code = 127
        error = f"执行命令时发生 {type(exc).__name__}：{exc}"
    elapsed = time.monotonic() - start
    completed_text = utc_now()
    result = CommandResult(
        command=argv,
        cwd=working_directory,
        started_at_utc=started_text,
        completed_at_utc=completed_text,
        elapsed_seconds=round(elapsed, 6),
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        error=error,
    )
    if log_dir is not None:
        _write_logs(log_dir, result, configured_env, timeout_seconds)
    return result


def _to_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _write_logs(
    log_dir: Path,
    result: CommandResult,
    configured_env: Mapping[str, str],
    timeout_seconds: float | None,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(log_dir / "stdout.log", result.stdout.encode("utf-8"))
    _atomic_write(log_dir / "stderr.log", result.stderr.encode("utf-8"))
    command_record = {
        "argv": list(result.command),
        "cwd": str(result.cwd),
        "env": _redacted_environment(configured_env),
        "timeout_seconds": timeout_seconds,
    }
    result_record = {
        "started_at_utc": result.started_at_utc,
        "completed_at_utc": result.completed_at_utc,
        "elapsed_seconds": result.elapsed_seconds,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "succeeded": result.succeeded,
        "error": result.error,
    }
    _atomic_write(log_dir / "command.json", json.dumps(command_record, indent=2, sort_keys=True).encode("utf-8"))
    _atomic_write(log_dir / "execution.json", json.dumps(result_record, indent=2, sort_keys=True).encode("utf-8"))
