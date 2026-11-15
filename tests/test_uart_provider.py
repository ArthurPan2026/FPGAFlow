from __future__ import annotations

from pathlib import Path
import unittest
import time
from types import MappingProxyType

from fpga_flow.models import FlowConfig, StageConfig
from fpga_flow.providers.uart import UARTCommandProvider


class FakePort:
    def __init__(self, responses):
        self.responses = responses
        self.buffer = bytearray()
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def in_waiting(self):
        return len(self.buffer)

    def write(self, value):
        command = value.decode("utf-8").strip()
        self.commands.append(command)
        self.buffer.extend(self.responses.get(command, b""))
        return len(value)

    def flush(self):
        return None

    def reset_input_buffer(self):
        self.buffer.clear()

    def read(self, size=1):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class DelayedFakePort(FakePort):
    def __init__(self, responses, delay_seconds):
        super().__init__(responses)
        self.delay_seconds = delay_seconds
        self.ready_at = 0.0

    @property
    def in_waiting(self):
        return len(self.buffer) if time.monotonic() >= self.ready_at else 0

    def write(self, value):
        command = value.decode("utf-8").strip()
        self.commands.append(command)
        self.ready_at = time.monotonic() + self.delay_seconds
        self.pending = self.responses.get(command, b"")
        return len(value)

    def read(self, size=1):
        if time.monotonic() < self.ready_at:
            return b""
        if getattr(self, "pending", b""):
            self.buffer.extend(self.pending)
            self.pending = b""
        return super().read(size)


class FakePortInfo:
    device = "COM5"
    serial_number = "FTDI-123"
    vid = 0x0403
    pid = 0x6010
    interface = "Interface 1"


class UARTProviderTests(unittest.TestCase):
    def _profile(self, root: Path, options, uart_profile=None):
        stage = StageConfig(
            name="board_smoke", kind="validate", needs=(), inputs=(), outputs=(), command=(),
            cwd=root, toolchains=(), env=MappingProxyType({}), target="board_a",
            runner="amd.uart.commands", options=MappingProxyType(options),
        )
        target_data = {"family": "amd-zynq-7000"}
        if uart_profile is not None:
            target_data["uart"] = uart_profile
        profile = FlowConfig(
            schema_version=1, project_name="uart-fixture", root=root,
            config_path=root / "fpga-flow.toml", output_dir=root / "out/fpga-flow",
            default_target="board_a", toolchains=MappingProxyType({}),
            targets=MappingProxyType({"board_a": MappingProxyType(target_data)}),
            stages=MappingProxyType({"board_smoke": stage}), suites=MappingProxyType({}),
        )
        return profile, stage

    def test_uart_commands_match_profile_assertions_on_windows_style_port(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            responses = {
                "status": b"[STAT] frame=1 miss=0 VDMA_ERR=0 DMA_ERR=0x000\r\n> ",
                "rom status": b"[CMD] rom loaded=1 bytes=8388608\r\n> ",
            }
            fake = FakePort(responses)
            options = {
                "steps": [
                    {"send": "status", "expect": [r"frame=1", r"VDMA_ERR=0"], "reject": [r"DMA_ERR=0x(?!000)"]},
                    {"send": "rom status", "expect": [r"rom loaded=1", r"bytes=8388608"]},
                ],
                "timeout_seconds": 0.2,
                "prompt_regex": r">\s*$",
            }
            profile, stage = self._profile(root, options, {"port": "COM5", "baudrate": 115200})
            provider = UARTCommandProvider(serial_factory=lambda **kwargs: fake)
            result = provider.execute(stage, profile, root / "run", "board_a")
            self.assertTrue(result.succeeded, result.stderr)
            self.assertEqual(fake.commands, ["status", "rom status"])
            self.assertIn("8388608", result.stdout)
            self.assertEqual(result.command[1], "COM5")

    def test_prompt_wait_survives_a_quiet_gap_longer_than_idle_threshold(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = DelayedFakePort({"status": b"late response OK>"}, 0.35)
            options = {
                "steps": [{"send": "status", "expect": ["late response OK"]}],
                "timeout_seconds": 1.0,
                "idle_seconds": 0.1,
                "prompt_regex": r">\s*$",
            }
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(stage, profile, root / "run", "board_a")
            self.assertTrue(result.succeeded, result.stderr)
            self.assertIn("late response OK", result.stdout)

    def test_usb_selector_requires_one_exact_match_and_reports_collision(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = {"steps": [{"send": "status", "expect": ["OK"]}], "timeout_seconds": 0.1}
            ports = [FakePortInfo()]
            fake = FakePort({"status": b"OK>"})
            profile, stage = self._profile(root, options, {"serial_number": "FTDI-123", "vid": "0x0403", "pid": "0x6010"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake,
                                         port_enumerator=lambda: ports).execute(stage, profile, root / "run", "board_a")
            self.assertTrue(result.succeeded, result.stderr)
            profile, stage = self._profile(root, options, {"serial_number": "FTDI-123"})
            with self.assertRaisesRegex(ValueError, "恰好匹配一个串口"):
                UARTCommandProvider(serial_factory=lambda **kwargs: fake,
                                    port_enumerator=lambda: ports + ports).execute(stage, profile, root / "run2", "board_a")

    def test_missing_expected_uart_response_fails_with_transcript(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakePort({"status": b"no status here>"})
            options = {"steps": [{"send": "status", "expect": ["VDMA_ERR=0"]}], "timeout_seconds": 0.2}
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(stage, profile, root / "run", "board_a")
            self.assertFalse(result.succeeded)
            self.assertIn("缺少预期匹配模式", result.error or "")
            self.assertIn("no status here", result.stdout)


if __name__ == "__main__":
    unittest.main()
