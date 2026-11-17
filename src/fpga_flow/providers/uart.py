"""跨平台、由 profile 驱动的 UART 命令验证提供器。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping

from ..engine import FlowError
from ..models import FlowConfig, StageConfig
from ..runner import CommandResult


class UARTProviderError(ValueError):
    """UART profile 无效或串口传输不可用时抛出。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sequence(value: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise UARTProviderError(f"{name} 必须是字符串或字符串数组")


def _compile_patterns(patterns: tuple[str, ...], where: str) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern, flags=re.MULTILINE)
        except re.error as exc:
            raise UARTProviderError(f"{where} 正则表达式无效 {pattern!r}：{exc}") from exc


def _regex_match(pattern: str, text: str, where: str) -> bool:
    try:
        return re.search(pattern, text, flags=re.MULTILINE) is not None
    except re.error as exc:
        raise UARTProviderError(f"{where} 正则表达式无效 {pattern!r}：{exc}") from exc


def _enumerate_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise UARTProviderError("UART 提供器需要可选依赖：pip install fpga-flow[uart]") from exc
    return list(list_ports.comports())


def _resolved_port_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _open_serial(port: str, *, baudrate: int, timeout: float, write_timeout: float):
    try:
        import serial
    except ImportError as exc:
        raise UARTProviderError("UART 提供器需要可选依赖：pip install fpga-flow[uart]") from exc
    return serial.Serial(port=port, baudrate=baudrate, timeout=timeout, write_timeout=write_timeout)


def _select_port(selector: Mapping[str, Any], port_enumerator: Callable[[], list] | None) -> str:
    direct = selector.get("port")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    ports = (port_enumerator or _enumerate_ports)()
    wanted_serial = selector.get("serial_number")
    wanted_vid = selector.get("vid")
    wanted_pid = selector.get("pid")
    wanted_interface = selector.get("interface")
    matches = []
    for info in ports:
        if wanted_serial is not None and getattr(info, "serial_number", None) != str(wanted_serial):
            continue
        if wanted_vid is not None and getattr(info, "vid", None) != int(str(wanted_vid), 0):
            continue
        if wanted_pid is not None and getattr(info, "pid", None) != int(str(wanted_pid), 0):
            continue
        if wanted_interface is not None and getattr(info, "interface", None) != str(wanted_interface):
            continue
        matches.append(info)
    if len(matches) != 1:
        raise UARTProviderError(f"UART 筛选器必须恰好匹配一个串口；实际匹配 {len(matches)}")
    device = getattr(matches[0], "device", None)
    if not isinstance(device, str) or not device:
        raise UARTProviderError("匹配到的 UART 串口没有设备路径")
    return device


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class UARTCommandProvider:
    """执行 profile 串口步骤，区分字面匹配与正则匹配。"""

    def __init__(self, *, serial_factory: Callable[..., Any] | None = None,
                 port_enumerator: Callable[[], list] | None = None):
        self.serial_factory = serial_factory or _open_serial
        self.port_enumerator = port_enumerator

    def _selector(self, stage: StageConfig, profile: FlowConfig, target: str | None) -> dict[str, Any]:
        target_data = profile.targets.get(target, {}) if target else {}
        target_uart = target_data.get("uart", {}) if isinstance(target_data, Mapping) else {}
        if not isinstance(target_uart, Mapping):
            target_uart = {}
        options = dict(stage.options)
        override = options.get("uart", {})
        selector = {**dict(target_uart), **dict(override)} if isinstance(override, Mapping) else dict(target_uart)
        return options, selector

    def preflight(self, stage: StageConfig, profile: FlowConfig, run_dir: Path,
                  target: str | None) -> dict[str, Any]:
        options, selector = self._selector(stage, profile, target)
        ports = (self.port_enumerator or _enumerate_ports)()
        direct = selector.get("port")
        if isinstance(direct, str) and direct.strip():
            direct_path = direct.strip()
            matches = [item for item in ports if getattr(item, "device", None) == direct_path]
            if not matches:
                canonical_direct = _resolved_port_path(direct_path)
                if canonical_direct is not None:
                    matches = [
                        item for item in ports
                        if _resolved_port_path(getattr(item, "device", None)) == canonical_direct
                    ]
            if len(matches) != 1:
                return {"status": "warning", "port": direct.strip(), "bytes_sent": 0,
                        "detail": "没有打开串口；配置路径未在设备枚举结果中唯一匹配"}
            port = direct.strip()
        else:
            port = _select_port(selector, lambda: ports)
        baudrate = int(options.get("baudrate", selector.get("baudrate", 115200)))
        return {"status": "ok", "port": port, "baudrate": baudrate, "bytes_sent": 0,
                "detail": "设备枚举唯一匹配；预检没有打开串口或发送字节"}

    def execute(self, stage: StageConfig, profile: FlowConfig, run_dir, target: str | None) -> CommandResult:
        options, selector = self._selector(stage, profile, target)
        port = _select_port(selector, self.port_enumerator)
        baudrate = int(options.get("baudrate", selector.get("baudrate", 115200)))
        default_timeout = float(options.get("timeout_seconds", 3.0))
        write_timeout = float(options.get("write_timeout_seconds", 2.0))
        line_ending = str(options.get("line_ending", "\r\n"))
        prompt_regex = str(options.get("prompt_regex", selector.get("prompt_regex", r"[>#]\s*$")))
        idle_seconds = float(options.get("idle_seconds", 0.15))
        steps = options.get("steps")
        if not isinstance(steps, (list, tuple)) or not steps:
            raise UARTProviderError("UART 提供器需要非空的 options.steps 数组")
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, Mapping):
                raise UARTProviderError(f"steps[{index}] 必须是 TOML 表")
            expected = _sequence(raw_step.get("expect", []), f"steps[{index}].expect", allow_empty=True)
            rejected = _sequence(raw_step.get("reject", []), f"steps[{index}].reject", allow_empty=True)
            _compile_patterns(expected, f"steps[{index}].expect")
            _compile_patterns(rejected, f"steps[{index}].reject")
            expected_text = _sequence(raw_step.get("expect_text", []), f"steps[{index}].expect_text", allow_empty=True)
            rejected_text = _sequence(raw_step.get("reject_text", []), f"steps[{index}].reject_text", allow_empty=True)
            if any(not text for text in (*expected_text, *rejected_text)):
                raise UARTProviderError(f"steps[{index}] 的 expect_text/reject_text 不能包含空字符串")
            if not expected and not expected_text and not bool(raw_step.get("allow_empty_response", False)):
                raise UARTProviderError(
                    f"steps[{index}] 必须声明 expect/expect_text，或设置 allow_empty_response=true"
                )
            step_prompt = str(raw_step.get("prompt_regex", prompt_regex))
            _compile_patterns((step_prompt,) if step_prompt else (), f"steps[{index}].prompt_regex")

        started = _now()
        monotonic_start = time.monotonic()
        stage_deadline = monotonic_start + float(stage.timeout_seconds) if stage.timeout_seconds else None
        transcript: list[str] = [f"串口={port} 波特率={baudrate}"]
        errors: list[str] = []
        timed_out = False
        completed_steps = 0
        bytes_sent = 0
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        _write_json(run_dir / "execution.json", {
            "state": "running", "started_at_utc": started, "stage": stage.name,
            "target": target, "step": 0, "step_count": len(steps), "bytes_sent": 0,
        })
        try:
            with self.serial_factory(port=port, baudrate=baudrate, timeout=0.05,
                                     write_timeout=write_timeout) as stream, \
                 stdout_path.open("a", encoding="utf-8", newline="") as out_log, \
                 stderr_path.open("a", encoding="utf-8", newline="") as err_log:
                if bool(options.get("reset_input_buffer", True)) and hasattr(stream, "reset_input_buffer"):
                    stream.reset_input_buffer()
                for index, raw_step in enumerate(steps):
                    if not isinstance(raw_step, Mapping):
                        raise UARTProviderError(f"steps[{index}] 必须是 TOML 表")
                    command = raw_step.get("send")
                    if not isinstance(command, str) or not command:
                        raise UARTProviderError(f"steps[{index}].send 必须是非空字符串")
                    step_timeout = float(raw_step.get("timeout_seconds", default_timeout))
                    if stage_deadline is not None:
                        remaining = stage_deadline - time.monotonic()
                        if remaining <= 0:
                            errors.append(f"steps[{index}] 超过 UART 阶段总超时限制")
                            timed_out = True
                            break
                        step_timeout = min(step_timeout, remaining)
                    expected = _sequence(raw_step.get("expect", []), f"steps[{index}].expect", allow_empty=True)
                    rejected = _sequence(raw_step.get("reject", []), f"steps[{index}].reject", allow_empty=True)
                    expected_text = _sequence(raw_step.get("expect_text", []),
                                               f"steps[{index}].expect_text", allow_empty=True)
                    rejected_text = _sequence(raw_step.get("reject_text", []),
                                              f"steps[{index}].reject_text", allow_empty=True)
                    step_header = f">>> [{index + 1}/{len(steps)}] {command}\n"
                    transcript.append(step_header.rstrip("\n"))
                    out_log.write(step_header)
                    out_log.flush()
                    _write_json(run_dir / "execution.json", {
                        "state": "running", "started_at_utc": started, "stage": stage.name,
                        "target": target, "step": index + 1, "step_count": len(steps),
                        "bytes_sent": sum(len(line.encode("utf-8")) for line in transcript),
                        "last_activity_at_utc": _now(),
                    })
                    encoded_command = (command + line_ending).encode("utf-8")
                    stream.write(encoded_command)
                    stream.flush()
                    bytes_sent += len(encoded_command)
                    response = bytearray()
                    deadline = time.monotonic() + step_timeout
                    last_data = time.monotonic()
                    step_prompt = str(raw_step.get("prompt_regex", prompt_regex))
                    require_prompt = bool(raw_step.get("require_prompt", bool(step_prompt)))
                    prompt_seen = False
                    while time.monotonic() < deadline:
                        waiting = int(getattr(stream, "in_waiting", 0))
                        chunk = stream.read(max(1, min(waiting, 4096)))
                        if chunk:
                            response.extend(chunk)
                            last_data = time.monotonic()
                            if step_prompt and _regex_match(step_prompt, response.decode("utf-8", errors="replace"),
                                                           "prompt"):
                                prompt_seen = True
                                break
                        elif response and not require_prompt and time.monotonic() - last_data >= idle_seconds:
                            break
                    response_text = response.decode("utf-8", errors="replace")
                    transcript.append(response_text)
                    out_log.write(response_text)
                    if response_text and not response_text.endswith("\n"):
                        out_log.write("\n")
                    out_log.flush()
                    transport_failed = require_prompt and not prompt_seen
                    if transport_failed:
                        error = f"步骤 {index + 1} 命令 {command!r} 在超时前未收到提示符"
                        errors.append(error)
                        err_log.write(error + "\n")
                        err_log.flush()
                        timed_out = True
                    assertion_errors = []
                    if not expected and not expected_text and not bool(raw_step.get("allow_empty_response", False)):
                        raise UARTProviderError(
                            f"steps[{index}] 必须声明 expect/expect_text，或设置 allow_empty_response=true"
                        )
                    assertion_errors.extend(
                        f"命令 {command!r} 的响应缺少预期正则 {pattern!r}"
                        for pattern in expected if not _regex_match(pattern, response_text, f"steps[{index}].expect")
                    )
                    assertion_errors.extend(
                        f"命令 {command!r} 的响应不包含预期文本 {text!r}"
                        for text in expected_text if text not in response_text
                    )
                    assertion_errors.extend(
                        f"命令 {command!r} 的响应匹配拒绝正则 {pattern!r}"
                        for pattern in rejected if _regex_match(pattern, response_text, f"steps[{index}].reject")
                    )
                    assertion_errors.extend(
                        f"命令 {command!r} 的响应包含拒绝文本 {text!r}"
                        for text in rejected_text if text in response_text
                    )
                    errors.extend(assertion_errors)
                    if assertion_errors:
                        err_log.write("\n".join(assertion_errors) + "\n")
                        err_log.flush()
                    if transport_failed:
                        break
                    continue_on_mismatch = bool(raw_step.get(
                        "continue_on_assertion_failure",
                        options.get("continue_on_assertion_failure", False),
                    ))
                    completed_steps = index + 1
                    if assertion_errors and not continue_on_mismatch:
                        break
        except UARTProviderError:
            raise
        except Exception as exc:
            error = f"UART I/O 在端口 {port} 上失败：{type(exc).__name__}：{exc}"
            errors.append(error)
            try:
                with stderr_path.open("a", encoding="utf-8") as err_log:
                    err_log.write(error + "\n")
            except OSError:
                pass
        elapsed = round(time.monotonic() - monotonic_start, 6)
        completed = _now()
        output = "\n".join(transcript) + "\n"
        error_text = "\n".join(errors)
        _write_json(run_dir / "execution.json", {
            "state": "failed" if errors else "succeeded",
            "started_at_utc": started, "completed_at_utc": completed,
            "elapsed_seconds": elapsed, "stage": stage.name, "target": target,
            "step_count": len(steps), "steps_completed": completed_steps,
            "bytes_sent": bytes_sent,
            "timed_out": timed_out,
        })
        return CommandResult(
            command=("uart", port, str(baudrate), f"{len(steps)}-commands"),
            cwd=profile.root,
            started_at_utc=started,
            completed_at_utc=completed,
            elapsed_seconds=elapsed,
            exit_code=1 if errors else 0,
            timed_out=timed_out,
            stdout=output,
            stderr=(error_text + "\n") if error_text else "",
            error=error_text or None,
        )
