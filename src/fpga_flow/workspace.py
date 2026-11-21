"""Resolve logical profile outputs to immutable per-run workspaces."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


def output_paths(record: Mapping[str, Any], patterns: Sequence[str]) -> tuple[str, ...]:
    logical = record.get("logical_output_root")
    physical = record.get("output_root")
    if not isinstance(logical, str) or not isinstance(physical, str):
        return tuple(patterns)
    return tuple(physical + pattern[len(logical):]
                 if pattern == logical or pattern.startswith(logical + "/")
                 else pattern for pattern in patterns)


def logical_path(record: Mapping[str, Any], relative: str) -> str:
    return output_paths(record, (relative,))[0]


def run_workspace(profile: Any, run_dir: Path) -> Path:
    return run_dir / "work" if profile.project_descriptor is not None else profile.output_dir
