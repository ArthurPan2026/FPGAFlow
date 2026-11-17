"""无 shell 命令执行器，持续保存可检查的运行证据。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Callable, Mapping, Sequence


_SECRET_WORDS = ("TOKEN", "PASSWORD", "PASSWD", "SECRET", "PRIVATE", "API_KEY", "ACCESS_KEY")
DEFAULT_CAPTURE_LIMIT_BYTES = 256 * 1024


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
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def _append_tail(current: bytearray, chunk: bytes, limit: int) -> bool:
    current.extend(chunk)
    if len(current) <= limit:
        return False
    del current[:len(current) - limit]
    return True


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """只终止本次 runner 启动的进程组，避免 Vivado 子进程脱离超时管理。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
    log_dir: Path | None = None,
    on_progress: Callable[[Mapping[str, object]], None] | None = None,
    progress_interval_seconds: float = 5.0,
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
) -> CommandResult:
    """直接执行 argv；stdout/stderr 在进程运行时持续写入日志文件。"""
    argv = tuple(str(item) for item in command)
    if not argv or not argv[0]:
        raise ValueError("command 的 argv 必须包含非空可执行文件")
    working_directory = cwd.resolve()
    if not working_directory.is_dir():
        raise ValueError(f"工作目录不存在：{working_directory}")
    if capture_limit_bytes < 1024:
        raise ValueError("capture_limit_bytes 不能小于 1024")

    child_env = os.environ.copy()
    configured_env = {str(key): str(value) for key, value in (env or {}).items()}
    child_env.update(configured_env)
    started_text = utc_now()
    start = time.monotonic()
    deadline = start + timeout_seconds if timeout_seconds is not None else None
    timed_out = False
    error: str | None = None
    exit_code = 0
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    totals = {"stdout": 0, "stderr": 0}
    last_activity = start
    state_lock = threading.Lock()
    readers: list[threading.Thread] = []
    reader_errors: list[str] = []
    reader_error_lock = threading.Lock()
    drain_deadline: float | None = None
    log_dir_path = Path(log_dir) if log_dir is not None else None
    stdout_file = None
    stderr_file = None

    if log_dir_path is not None:
        log_dir_path.mkdir(parents=True, exist_ok=True)
        stdout_file = (log_dir_path / "stdout.log").open("wb")
        stderr_file = (log_dir_path / "stderr.log").open("wb")
        _write_json(log_dir_path / "command.json", {
            "argv": list(argv),
            "cwd": str(working_directory),
            "env": _redacted_environment(configured_env),
            "timeout_seconds": timeout_seconds,
        })

    process: subprocess.Popen[bytes] | None = None
    try:
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(
            list(argv), cwd=working_directory, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, bufsize=0, **kwargs,
        )
    except OSError as exc:
        error = f"执行命令时发生 {type(exc).__name__}：{exc}"
        exit_code = 127
        if stdout_file is not None:
            stdout_file.close()
            stderr_file.close()
        completed_text = utc_now()
        result = CommandResult(argv, working_directory, started_text, completed_text,
                               round(time.monotonic() - start, 6), exit_code, False,
                               "", "", error)
        if log_dir_path is not None:
            _write_logs(log_dir_path, result, configured_env, timeout_seconds,
                        include_streams=False)
        return result

    if log_dir_path is not None:
        _write_json(log_dir_path / "execution.json", {
            "started_at_utc": started_text,
            "pid": process.pid,
            "state": "running",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "last_activity_monotonic": 0.0,
        })

    def pump(name: str, pipe, file_obj) -> None:
        nonlocal last_activity
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if file_obj is not None:
                    file_obj.write(chunk)
                    file_obj.flush()
                with state_lock:
                    totals[name] += len(chunk)
                    truncated[name] = _append_tail(tails[name], chunk, capture_limit_bytes)
                    last_activity = time.monotonic()
        except OSError as exc:
            with reader_error_lock:
                reader_errors.append(f"{name} 管道读取失败：{type(exc).__name__}：{exc}")
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=pump, args=("stdout", process.stdout, stdout_file), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr, stderr_file), daemon=True),
    ]
    for thread in readers:
        thread.start()

    next_progress = start + max(0.25, progress_interval_seconds)
    next_state_write = start
    while process.poll() is None or any(thread.is_alive() for thread in readers):
        now = time.monotonic()
        if deadline is not None and now >= deadline and process.poll() is None:
            timed_out = True
            error = f"命令运行超过超时限制 {timeout_seconds} 秒"
            _terminate_process_tree(process)
            drain_deadline = time.monotonic() + 5.0
        if log_dir_path is not None and now >= next_state_write:
            with state_lock:
                snapshot = dict(totals)
                activity = last_activity
            _write_json(log_dir_path / "execution.json", {
                "started_at_utc": started_text,
                "pid": process.pid,
                "state": "running" if process.poll() is None else "draining",
                "stdout_bytes": snapshot["stdout"],
                "stderr_bytes": snapshot["stderr"],
                "last_activity_monotonic": round(activity - start, 3),
                "heartbeat_at_utc": utc_now(),
            })
            next_state_write = now + 1.0
        if timed_out and drain_deadline is not None and now >= drain_deadline:
            for pipe in (process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            break
        if on_progress is not None and now >= next_progress:
            with state_lock:
                snapshot = dict(totals)
                activity = last_activity
            try:
                on_progress({
                    "pid": process.pid,
                    "elapsed_seconds": round(now - start, 1),
                    "stdout_bytes": snapshot["stdout"],
                    "stderr_bytes": snapshot["stderr"],
                    "last_output_seconds_ago": round(now - activity, 1),
                })
            except Exception:
                pass
            next_progress = now + max(0.25, progress_interval_seconds)
        time.sleep(0.05)

    for thread in readers:
        thread.join(timeout=2.0)
    with reader_error_lock:
        if reader_errors and error is None:
            error = "; ".join(reader_errors)
    if stdout_file is not None:
        stdout_file.flush()
        os.fsync(stdout_file.fileno())
        stdout_file.close()
        stderr_file.flush()
        os.fsync(stderr_file.fileno())
        stderr_file.close()

    exit_code = process.returncode if process.returncode is not None else 1
    if timed_out:
        exit_code = 124
    completed_text = utc_now()
    elapsed = round(time.monotonic() - start, 6)
    result = CommandResult(
        command=argv,
        cwd=working_directory,
        started_at_utc=started_text,
        completed_at_utc=completed_text,
        elapsed_seconds=elapsed,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=bytes(tails["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(tails["stderr"]).decode("utf-8", errors="replace"),
        error=error,
        stdout_truncated=truncated["stdout"],
        stderr_truncated=truncated["stderr"],
        stdout_bytes=totals["stdout"],
        stderr_bytes=totals["stderr"],
    )
    if log_dir_path is not None:
        _write_logs(log_dir_path, result, configured_env, timeout_seconds,
                    include_streams=False)
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
    *,
    include_streams: bool = True,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    if include_streams:
        _atomic_write(log_dir / "stdout.log", result.stdout.encode("utf-8"))
        _atomic_write(log_dir / "stderr.log", result.stderr.encode("utf-8"))
    command_record = {
        "argv": list(result.command),
        "cwd": str(result.cwd),
        "env": _redacted_environment(configured_env),
        "timeout_seconds": timeout_seconds,
    }
    result_record = {
        "state": "succeeded" if result.succeeded else "failed",
        "started_at_utc": result.started_at_utc,
        "completed_at_utc": result.completed_at_utc,
        "elapsed_seconds": result.elapsed_seconds,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "succeeded": result.succeeded,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "error": result.error,
    }
    _write_json(log_dir / "command.json", command_record)
    _write_json(log_dir / "execution.json", result_record)
