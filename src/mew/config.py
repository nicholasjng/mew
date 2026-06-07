"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from mew._typing import TimeUnitStr

_VALID_UNITS = frozenset(get_args(TimeUnitStr))


@dataclass(slots=True)
class Config:
    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    # Keys are the short flag name (without the `--benchmark_` prefix); values
    # become `--benchmark_<key>=<value>` (or `--benchmark_<key>` for bool True).
    benchmark_options: dict[str, Any] = field(default_factory=dict)
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
        benchmark_options = dict(tool.get("benchmark_options", {}))
        if (unit := benchmark_options.get("unit")) is not None and unit not in _VALID_UNITS:
            raise ValueError(
                f"invalid time unit {unit!r} in [tool.mew.benchmark_options]; "
                f"expected one of {sorted(_VALID_UNITS)}"
            )
        return Config(
            benchpaths=list(tool.get("benchpaths", ["benchmarks"])),
            python_files=list(tool.get("python_files", ["bench_*.py", "*_bench.py"])),
            benchmark_options=benchmark_options,
            project_root=parent,
        )
    return Config()


def format_benchmark_args(options: dict[str, Any]) -> list[str]:
    """Translate a ``{key: value}`` mapping into Google Benchmark CLI flags.

    Bool ``True`` becomes a bare ``--benchmark_<key>``; ``False`` is omitted.
    Everything else is serialized as ``--benchmark_<key>=<value>``.
    """
    args: list[str] = []
    for key, value in options.items():
        flag = f"--benchmark_{key}"
        if value is True:
            args.append(flag)
        elif value is False:
            continue
        else:
            args.append(f"{flag}={value}")
    return args
