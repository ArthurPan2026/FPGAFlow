"""可复用的 AMD Zynq JTAG/QSPI 命令构造器。

本模块只包含工具与板卡协议机制。器件和板卡名称、
Flash 几何参数及目标筛选条件均由消费项目 profile 提供。
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable, Mapping


class DeployCommandError(ValueError):
    """目标选择无效或部署配置不安全时抛出。"""


_TARGET_LINE = re.compile(
    r"^\s*(?P<id>\d+)\s+(?P<target>.+?)\s+\(name\s+(?P<device>\S+)\s+idcode\s+(?P<idcode>[0-9a-fA-F]+)\)\s*$"
)


def parse_program_flash_targets(output: str) -> list[dict[str, str]]:
    """解析 ``program_flash -jtagtargets`` 输出，不假设特定板卡名称。"""
    result: list[dict[str, str]] = []
    for line in output.splitlines():
        match = _TARGET_LINE.match(line)
        if match:
            result.append({
                "id": match.group("id"),
                "target_name": match.group("target").strip(),
                "device_name": match.group("device"),
                "idcode": match.group("idcode").lower(),
            })
    return result


def select_unique_flash_target(
    targets: Iterable[Mapping[str, str]],
    *,
    device_name: str,
    target_name_contains: str | None = None,
    idcode: str | None = None,
) -> Mapping[str, str]:
    """依据 profile 提供的身份条件选择唯一目标；无法唯一确定时拒绝继续。"""
    matches = [dict(item) for item in targets if item.get("device_name") == device_name]
    if target_name_contains:
        matches = [item for item in matches if target_name_contains in item.get("target_name", "")]
    if idcode:
        expected = idcode.lower().removeprefix("0x")
        matches = [item for item in matches if item.get("idcode", "").lower().removeprefix("0x") == expected]
    if len(matches) != 1:
        raise DeployCommandError(
            f"JTAG 目标必须恰好匹配一个目标；设备={device_name!r}，"
            f"名称包含={target_name_contains!r}，idcode={idcode!r}；实际匹配={matches}"
        )
    return matches[0]


def program_flash_query_argv(program_flash: str | Path, server_url: str) -> list[str]:
    if not str(server_url).strip():
        raise DeployCommandError("hw_server URL 不能为空")
    return [str(program_flash), "-jtagtargets", "-url", str(server_url)]


def build_qspi_program_argv(
    program_flash: str | Path,
    boot_image: str | Path,
    fsbl: str | Path,
    *,
    flash_type: str,
    offset: int,
    target_name: str,
    server_url: str,
) -> list[str]:
    """构造范围受限的 QSPI 写入和读回命令；绝不请求整片擦除。"""
    if offset < 0:
        raise DeployCommandError("QSPI 偏移量不能为负数")
    if not flash_type.strip() or not target_name.strip() or not server_url.strip():
        raise DeployCommandError("必须提供 Flash 类型、唯一目标名称和 hw_server URL")
    return [
        str(program_flash), "-f", str(boot_image), "-fsbl", str(fsbl),
        "-flash_type", flash_type, "-offset", str(offset),
        "-blank_check", "-verify", "-target_name", target_name,
        "-url", server_url,
    ]


def tcl_quote(value: str | Path) -> str:
    """为 Tcl 字面量加引号，包括含空格的 Windows 路径。"""
    text = str(value)
    if "\n" in text or "\r" in text or "\x00" in text:
        raise DeployCommandError("Tcl 字面量不能包含换行符或 NUL")
    return '"' + (text.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")) + '"'


def _tcl_list(values: Iterable[str]) -> str:
    return "[list " + " ".join(tcl_quote(value) for value in values) + "]"



def render_zynq_jtag_probe_tcl(
    *, hw_server_url: str, ps_target_pattern: str, pl_target_pattern: str,
    cpu_target_pattern: str,
) -> str:
    """生成只查询 JTAG 目标的 Tcl，不复位或配置器件。"""
    values = {
        "hw_server_url": hw_server_url,
        "ps_target_pattern": ps_target_pattern,
        "pl_target_pattern": pl_target_pattern,
        "cpu_target_pattern": cpu_target_pattern,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise DeployCommandError("JTAG 预检需要 hw_server URL 和三个目标匹配模式")
    return f'''proc check_unique_target {{pattern label}} {{
    set matches {{}}
    foreach properties [targets -target-properties] {{
        if {{[string match $pattern [dict get $properties name]]}} {{
            lappend matches $properties
        }}
    }}
    if {{[llength $matches] != 1}} {{
        error "$label 目标必须恰好匹配一个目标；pattern={{$pattern}} matches=$matches"
    }}
    set properties [lindex $matches 0]
    puts "PREFLIGHT_TARGET_OK $label id=[dict get $properties target_id] name=[dict get $properties name]"
}}
connect -url {tcl_quote(hw_server_url)}
check_unique_target {tcl_quote(ps_target_pattern)} PS
check_unique_target {tcl_quote(pl_target_pattern)} PL
check_unique_target {tcl_quote(cpu_target_pattern)} CPU
disconnect
puts "PREFLIGHT_READ_ONLY"
exit
'''

def render_zynq_jtag_program_tcl(
    bitstream: str | Path,
    ps_init_script: str | Path,
    elf: str | Path,
    *,
    hw_server_url: str,
    run_ms: int = 1000,
    ps_target_pattern: str = "APU",
    pl_target_pattern: str = "xc7z020*",
    cpu_target_pattern: str = "ARM Cortex-A9 MPCore #0",
    ps_init_commands: Iterable[str] = ("ps7_init", "ps7_post_config"),
) -> str:
    """生成由 profile 参数化的 Zynq 联合 JTAG 脚本。

    消费项目 profile 提供板卡/SoC 目标筛选模式和 PS 初始化
    过程名称。本模块不嵌入具体项目路径或板卡名称。
    """
    if run_ms < 0:
        raise DeployCommandError("run_ms 不能为负数")
    if not hw_server_url.strip():
        raise DeployCommandError("hw_server URL 不能为空")
    for path in (bitstream, ps_init_script, elf):
        if not Path(path).is_file():
            raise DeployCommandError(f"必需的部署产物不存在：{path}")
    init_commands = tuple(ps_init_commands)
    if not init_commands or any(not command.strip() for command in init_commands):
        raise DeployCommandError("PS 初始化命令列表不能为空")
    return f'''set bit_file {tcl_quote(Path(bitstream).resolve())}
set ps_init_file {tcl_quote(Path(ps_init_script).resolve())}
set elf_file {tcl_quote(Path(elf).resolve())}
set hw_server_url {tcl_quote(hw_server_url)}
set run_ms {int(run_ms)}
set ps_target_pattern {tcl_quote(ps_target_pattern)}
set pl_target_pattern {tcl_quote(pl_target_pattern)}
set cpu_target_pattern {tcl_quote(cpu_target_pattern)}
set ps_init_commands {_tcl_list(init_commands)}

proc select_unique_target {{pattern label}} {{
    set matches {{}}
    foreach properties [targets -target-properties] {{
        if {{[string match $pattern [dict get $properties name]]}} {{
            lappend matches $properties
        }}
    }}
    if {{[llength $matches] != 1}} {{
        error "$label 目标必须恰好匹配一个目标；pattern={{$pattern}} matches=$matches"
    }}
    set properties [lindex $matches 0]
    set target_id [dict get $properties target_id]
    set target_name [dict get $properties name]
    targets -set $target_id
    puts "TARGET_SELECTED $label id=$target_id name=$target_name"
}}

connect -url $hw_server_url
select_unique_target $ps_target_pattern PS
rst -system
after 1000
select_unique_target $pl_target_pattern PL
fpga $bit_file
puts "PL_CONFIGURED"
select_unique_target $ps_target_pattern PS
source $ps_init_file
foreach init_command $ps_init_commands {{ uplevel #0 $init_command }}
puts "PS_INITIALIZED"
select_unique_target $cpu_target_pattern CPU
rst -processor
dow $elf_file
puts "APP_ELF_DOWNLOADED"
con
puts "APP_RUNNING"
after $run_ms
puts "APP_RAN_[format %d $run_ms]MS"
select_unique_target $cpu_target_pattern CPU
puts "APP_PC=[rrd pc]"
exit
'''
