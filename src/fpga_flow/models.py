"""为 CLI 与适配器提供共用的不可变配置模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ToolchainConfig:
    name: str
    command: tuple[str, ...]
    version_args: tuple[str, ...]
    expected_version: str | None = None
    version_command: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class StageConfig:
    name: str
    kind: str
    needs: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    command: tuple[str, ...]
    cwd: Path
    toolchains: tuple[str, ...]
    env: Mapping[str, str]
    timeout_seconds: int | None = None
    success_markers: tuple[str, ...] = ()
    failure_markers: tuple[str, ...] = ()
    target: str | None = None
    destructive: bool = False
    description: str = ""
    runner: str = "command"
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FlowConfig:
    schema_version: int
    project_name: str
    root: Path
    config_path: Path
    output_dir: Path
    default_target: str | None
    toolchains: Mapping[str, ToolchainConfig]
    targets: Mapping[str, Mapping[str, Any]]
    stages: Mapping[str, StageConfig]
    suites: Mapping[str, tuple[str, ...]]
