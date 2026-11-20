"""跨进程项目运行锁。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Mapping


class RunLockError(RuntimeError):
    """项目输出目录正由另一个 Flow 操作占用。"""


_registry_guard = threading.Lock()
_process_locks: dict[str, threading.Lock] = {}


def _process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _registry_guard:
        return _process_locks.setdefault(key, threading.Lock())


class ProjectRunLock:
    """用操作系统文件锁保护同一输出目录，并记录当前所有者。"""

    def __init__(
        self,
        path: Path,
        *,
        action: str,
        command: str | None = None,
        wait: bool = False,
        poll_seconds: float = 0.2,
    ) -> None:
        self.path = path
        self.action = action
        self.command = command
        self.wait = wait
        self.poll_seconds = poll_seconds
        self._handle = None
        self._metadata_guard = threading.RLock()
        self._thread_lock = _process_lock(path)
        self._thread_locked = False
        self._active_stages: dict[str, str] = {}
        self.metadata: dict[str, Any] = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "action": action,
            "command": command,
            "started_at_utc": None,
            "stage": None,
            "run_id": None,
        }

    def __enter__(self) -> "ProjectRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.wait:
            self._thread_lock.acquire()
            self._thread_locked = True
        elif not self._thread_lock.acquire(blocking=False):
            owner = self.read_owner(self.path)
            raise RunLockError(self._busy_message(owner))
        else:
            self._thread_locked = True
        try:
            flags = os.O_CREAT | os.O_RDWR
            fd = os.open(self.path, flags, 0o600)
            self._handle = os.fdopen(fd, "r+b", buffering=0)
            if os.fstat(fd).st_size == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            while True:
                try:
                    self._lock_os_file(blocking=False)
                    break
                except (BlockingIOError, OSError) as exc:
                    if not self._is_lock_contention(exc):
                        raise
                    if not self.wait:
                        raise RunLockError(self._busy_message(self.read_owner(self.path))) from exc
                    time.sleep(self.poll_seconds)
            self.metadata["started_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write_owner()
            return self
        except Exception:
            self._close_and_unlock()
            raise

    def set_stage(self, stage: str, run_id: str | None = None) -> None:
        with self._metadata_guard:
            key = run_id or stage
            self._active_stages[key] = stage
            self.metadata["active_stages"] = [
                {"run_id": run, "stage": name} for run, name in sorted(self._active_stages.items())
            ]
            self.metadata["stage"] = stage if len(self._active_stages) == 1 else f"{len(self._active_stages)} stages"
            if run_id is not None:
                self.metadata["run_id"] = run_id
            self._write_owner()

    def set_run(self, run_id: str) -> None:
        with self._metadata_guard:
            self.metadata["run_id"] = run_id
            self._write_owner()

    def clear_stage(self, run_id: str) -> None:
        with self._metadata_guard:
            self._active_stages.pop(run_id, None)
            self.metadata["active_stages"] = [
                {"run_id": run, "stage": name} for run, name in sorted(self._active_stages.items())
            ]
            self.metadata["stage"] = (
                next(iter(self._active_stages.values())) if len(self._active_stages) == 1
                else (f"{len(self._active_stages)} stages" if self._active_stages else None)
            )
            self._write_owner()

    def _write_owner(self) -> None:
        if self._handle is None:
            return
        payload = json.dumps(self.metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._handle.seek(1)
        self._handle.truncate(1)
        self._handle.write(payload)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    @staticmethod
    def read_owner(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("rb") as stream:
                stream.seek(1)
                raw = stream.read()
            value = json.loads(raw.decode("utf-8")) if raw else None
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_lock_contention(exc: OSError) -> bool:
        if isinstance(exc, BlockingIOError):
            return True
        if os.name == "nt":
            # msvcrt.locking can surface an existing cross-process byte lock as
            # PermissionError/errno 13 on hosted Windows runners.
            return (
                getattr(exc, "winerror", None) in {13, 33, 36}
                or getattr(exc, "errno", None) == 13
            )
        return getattr(exc, "errno", None) in {errno_value("EAGAIN"), errno_value("EWOULDBLOCK")}

    @staticmethod
    def _busy_message(owner: Mapping[str, Any] | None) -> str:
        if not owner:
            return "项目输出目录正被另一个 Flow 操作占用；稍后重试，或显式使用 --wait-lock"
        return (
            "项目输出目录正被另一个 Flow 操作占用："
            f"host={owner.get('host') or '?'} pid={owner.get('pid') or '?'} "
            f"action={owner.get('action') or '?'} stage={owner.get('stage') or '-'} "
            f"run_id={owner.get('run_id') or '-'} active={owner.get('active_stages') or []}；"
            "稍后重试，或显式使用 --wait-lock"
        )

    def _lock_os_file(self, *, blocking: bool) -> None:
        if os.name == "nt":
            import msvcrt
            self._handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(self._handle.fileno(), mode, 1)
        else:
            import fcntl
            mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(self._handle.fileno(), mode)

    def _unlock_os_file(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def _close_and_unlock(self) -> None:
        if self._handle is not None:
            self._unlock_os_file()
            self._handle.close()
            self._handle = None
        if self._thread_locked:
            self._thread_lock.release()
            self._thread_locked = False

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._close_and_unlock()


def errno_value(name: str) -> int:
    import errno
    return int(getattr(errno, name, -1))
