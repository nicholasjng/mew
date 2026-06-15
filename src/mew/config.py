"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from mew._typing import TimeUnitStr

_VALID_UNITS = frozenset(get_args(TimeUnitStr))


@dataclass(slots=True, frozen=True)
class SessionTagSpec:
    """Whether and how to derive the auto session tag.

    ``enabled`` gates auto-derivation (an explicit ``--session-tag`` is always honored).
    ``tool``/``args`` are the command: both ``None`` → derive automatically (jj, then
    git); a ``tool`` with no ``args`` uses the built-in preset for ``git``/``jj`` (none
    for any other command).
    """

    enabled: bool = True
    tool: str | None = None
    args: list[str] | None = None


@dataclass(slots=True)
class Config:
    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    # Keys are the short flag name (without the `--benchmark_` prefix); values
    # become `--benchmark_<key>=<value>` (or `--benchmark_<key>` for bool True).
    benchmark_options: dict[str, Any] = field(default_factory=dict)
    session_tag: SessionTagSpec = field(default_factory=SessionTagSpec)
    project_root: Path | None = None


def _snake_keys(obj: Any) -> Any:
    """Recursively rewrite dict keys ``kebab-case`` -> ``snake_case``.

    Config keys are written with dashes (TOML idiom) but map straight onto the
    snake_case :class:`Config` fields, so coerce once after the read.
    """
    if isinstance(obj, dict):
        return {k.replace("-", "_"): _snake_keys(v) for k, v in obj.items()}
    return obj


def _parse_session_tag(raw: Any) -> SessionTagSpec:
    if not isinstance(raw, dict):
        raise ValueError("[tool.mew.session-tag] must be a table")
    if unknown := set(raw) - {"enabled", "tool", "args"}:
        raise ValueError(f"unknown keys in [tool.mew.session-tag]: {sorted(unknown)}")
    enabled, tool, args = raw.get("enabled", True), raw.get("tool"), raw.get("args")
    if not isinstance(enabled, bool):
        raise ValueError("[tool.mew.session-tag] enabled must be a boolean")
    if tool is not None and not isinstance(tool, str):
        raise ValueError("[tool.mew.session-tag] tool must be a string")
    if args is None:
        return SessionTagSpec(enabled=enabled, tool=tool, args=None)
    if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
        raise ValueError("[tool.mew.session-tag] args must be a list of strings")
    return SessionTagSpec(enabled=enabled, tool=tool, args=[str(a) for a in args])


def load(start: Path | None = None) -> Config:
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        tool = _snake_keys(data.get("tool", {}).get("mew", {}))
        benchmark_options = dict(tool.get("benchmark_options", {}))
        if (unit := benchmark_options.get("unit")) is not None and unit not in _VALID_UNITS:
            raise ValueError(
                f"invalid time unit {unit!r} in [tool.mew.benchmark-options]; "
                f"expected one of {sorted(_VALID_UNITS)}"
            )
        return Config(
            benchpaths=list(tool.get("benchpaths", ["benchmarks"])),
            python_files=list(tool.get("python_files", ["bench_*.py", "*_bench.py"])),
            benchmark_options=benchmark_options,
            session_tag=_parse_session_tag(tool.get("session_tag", {})),
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
