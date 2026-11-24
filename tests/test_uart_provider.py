from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
import time
from types import MappingProxyType, SimpleNamespace

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


class StalePromptFakePort(DelayedFakePort):
    @property
    def in_waiting(self):
        if self.buffer:
            return len(self.buffer)
        return super().in_waiting

    def read(self, size=1):
        if self.buffer:
            return FakePort.read(self, size)
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

    def test_stale_prompt_is_ignored_until_the_current_command_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = StalePromptFakePort(
                {"status": b"[INPUT] detached gen=6\\r\\nstatus\r\n[STAT] frame=2\r\n> "},
                delay_seconds=0.05,
            )
            fake.buffer.extend(b"> \r\n")
            options = {
                "steps": [{"send": "status", "expect_text": ["[STAT] frame=2"]}],
                "timeout_seconds": 0.5,
                "prompt_regex": r">\s*$",
                "require_command_echo": True,
                "reset_input_buffer": False,
            }
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(
                stage, profile, root / "run", "board_a"
            )
            self.assertTrue(result.succeeded, result.stderr)
            self.assertEqual(fake.commands, ["status"])
            self.assertIn("[STAT] frame=2", result.stdout)

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

    def test_literal_text_expectation_handles_brackets(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakePort({"load": b"[CMD] rom load ok\r\n> "})
            options = {
                "steps": [{"send": "load", "expect_text": ["[CMD] rom load ok"]}],
                "prompt_regex": r">\s*$",
            }
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(
                stage, profile, root / "run", "board_a"
            )
            self.assertTrue(result.succeeded, result.stderr)

    def test_transport_timeout_stops_later_uart_steps(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakePort({"first": b"partial response"})
            options = {
                "steps": [
                    {"send": "first", "expect_text": ["done"], "timeout_seconds": 0.08,
                     "prompt_regex": r"READY>\s*$"},
                    {"send": "second", "expect_text": ["ok"], "prompt_regex": r">\s*$"},
                ],
            }
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(
                stage, profile, root / "run", "board_a"
            )
            self.assertFalse(result.succeeded)
            self.assertEqual(fake.commands, ["first"])
            self.assertTrue(result.timed_out)

    def test_assertion_mismatch_continues_only_when_profile_allows_it(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakePort({"first": b"not-ready\r\n> ", "second": b"ok\r\n> "})
            options = {
                "continue_on_assertion_failure": True,
                "steps": [
                    {"send": "first", "expect_text": ["success"]},
                    {"send": "second", "expect_text": ["ok"]},
                ],
                "prompt_regex": r">\s*$",
            }
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(
                stage, profile, root / "run", "board_a"
            )
            self.assertFalse(result.succeeded)
            self.assertEqual(fake.commands, ["first", "second"])
            self.assertIn("first", result.stderr)

    def test_uart_preflight_enumerates_without_opening_or_sending(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile, stage = self._profile(
                root, {"steps": [{"send": "status", "expect_text": ["ok"]}]},
                {"port": "COM5"},
            )
            def unexpected_open(**kwargs):
                raise AssertionError("preflight must not open the port")
            result = UARTCommandProvider(
                serial_factory=unexpected_open,
                port_enumerator=lambda: [FakePortInfo()],
            ).preflight(stage, profile, root / "preflight", "board_a")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["bytes_sent"], 0)

    @unittest.skipUnless(os.name == "posix", "requires POSIX symlink semantics")
    def test_uart_preflight_resolves_stable_symlink_to_enumerated_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / "ttyUSB1"
            device.touch()
            stable_alias = root / "by-id" / "usb-device-port0"
            stable_alias.parent.mkdir()
            stable_alias.symlink_to(device)
            profile, stage = self._profile(
                root, {"steps": [{"send": "status", "expect_text": ["ok"]}]},
                {"port": str(stable_alias)},
            )

            def unexpected_open(**kwargs):
                raise AssertionError("preflight must not open the serial port")

            result = UARTCommandProvider(
                serial_factory=unexpected_open,
                port_enumerator=lambda: [SimpleNamespace(device=str(device))],
            ).preflight(stage, profile, root / "preflight", "board_a")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["port"], str(stable_alias))
            self.assertEqual(result["bytes_sent"], 0)

    def test_missing_expected_uart_response_fails_with_transcript(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakePort({"status": b"no status here>"})
            options = {"steps": [{"send": "status", "expect": ["VDMA_ERR=0"]}], "timeout_seconds": 0.2}
            profile, stage = self._profile(root, options, {"port": "COM5"})
            result = UARTCommandProvider(serial_factory=lambda **kwargs: fake).execute(stage, profile, root / "run", "board_a")
            self.assertFalse(result.succeeded)
            self.assertIn("缺少预期正则", result.error or "")
            self.assertIn("no status here", result.stdout)


if __name__ == "__main__":
    unittest.main()
