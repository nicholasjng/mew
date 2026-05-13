"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Config:
    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    project_root: Path | None = None


def load(start: Path | None = None) -> Config:
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        tool = data.get("tool", {}).get("mew", {})
        return Config(
            benchpaths=list(tool.get("benchpaths", ["benchmarks"])),
            python_files=list(tool.get("python_files", ["bench_*.py", "*_bench.py"])),
            project_root=parent,
        )
    return Config()
