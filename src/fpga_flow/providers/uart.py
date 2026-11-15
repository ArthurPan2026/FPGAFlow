"""跨平台、由 profile 驱动的 UART 命令验证提供器。

PySerial 为可选依赖，仅在调用本提供器时导入。
项目专属命令名称和通过/失败标记保留在 profile 中。
"""
from __future__ import annotations

from datetime import datetime, timezone
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


class UARTCommandProvider:
    """执行 profile 声明的串口命令，并检查响应是否匹配预期模式。"""

    def __init__(self, *, serial_factory: Callable[..., Any] | None = None,
                 port_enumerator: Callable[[], list] | None = None):
        self.serial_factory = serial_factory or _open_serial
        self.port_enumerator = port_enumerator

    def execute(self, stage: StageConfig, profile: FlowConfig, run_dir, target: str | None) -> CommandResult:
        options = dict(stage.options)
        target_data = profile.targets.get(target, {}) if target else {}
        target_uart = target_data.get("uart", {}) if isinstance(target_data, Mapping) else {}
        if not isinstance(target_uart, Mapping):
            target_uart = {}
        selector = {**dict(target_uart), **dict(options.get("uart", {}))} if isinstance(options.get("uart", {}), Mapping) else dict(target_uart)
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
        started = _now()
        monotonic_start = time.monotonic()
        stage_deadline = monotonic_start + float(stage.timeout_seconds) if stage.timeout_seconds else None
        transcript: list[str] = [f"串口={port} 波特率={baudrate}"]
        errors: list[str] = []
        timed_out = False
        try:
            with self.serial_factory(port=port, baudrate=baudrate, timeout=0.05,
                                     write_timeout=write_timeout) as stream:
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
                            errors.append("UART 阶段超过总超时限制")
                            timed_out = True
                            break
                        step_timeout = min(step_timeout, remaining)
                    expected = _sequence(raw_step.get("expect"), f"steps[{index}].expect", allow_empty=True)
                    rejected = _sequence(raw_step.get("reject"), f"steps[{index}].reject", allow_empty=True)
                    stream.write((command + line_ending).encode("utf-8"))
                    stream.flush()
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
                            if step_prompt and _regex_match(step_prompt, response.decode("utf-8", errors="replace"), "prompt"):
                                prompt_seen = True
                                break
                        elif response and not require_prompt and time.monotonic() - last_data >= idle_seconds:
                            break
                    response_text = response.decode("utf-8", errors="replace")
                    transcript.extend([f">>> {command}", response_text])
                    if require_prompt and not prompt_seen:
                        errors.append(f"命令 {command!r} 的响应在配置的提示符出现前超时")
                        timed_out = True
                    if not expected and not bool(raw_step.get("allow_empty_response", False)):
                        raise UARTProviderError(f"steps[{index}] 必须声明 expect 模式，或设置 allow_empty_response=true")
                    for pattern in expected:
                        if not _regex_match(pattern, response_text, f"steps[{index}].expect"):
                            errors.append(f"命令 {command!r} 的响应缺少预期匹配模式 {pattern!r}")
                    for pattern in rejected:
                        if _regex_match(pattern, response_text, f"steps[{index}].reject"):
                            errors.append(f"命令 {command!r} 的响应匹配了拒绝模式 {pattern!r}")
        except UARTProviderError:
            raise
        except Exception as exc:
            errors.append(f"UART I/O 在端口 {port} 上失败：{type(exc).__name__}：{exc}")
        elapsed = round(time.monotonic() - monotonic_start, 6)
        completed = _now()
        output = "\n".join(transcript) + "\n"
        error_text = "\n".join(errors)
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
