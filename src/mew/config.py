"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Config:
    """Resolved ``[tool.mew]`` settings.

    Attributes
    ----------
    benchpaths : list[str]
        Directories searched when no path argument is given, relative to
        ``project_root``.
    python_files : list[str]
        Glob patterns identifying benchmark files during discovery.
    setup : str or None
        Python file imported once before any benchmark file, relative to
        ``project_root``. Runs whatever the project needs set up run-wide --
        typically context providers, so provenance does not depend on which
        benchmark files a given invocation happens to select.
    statistic : str or None
        Default ``mew compare`` reducer; ``None`` keeps the median.
    project_root : Path or None
        Directory of the ``pyproject.toml`` these settings came from; ``None``
        when no file was found and defaults are in use.
    """

    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    setup: str | None = None
    statistic: str | None = None
    project_root: Path | None = None


def _snake_keys(obj: Any) -> Any:
    """Recursively rewrite dict keys ``kebab-case`` -> ``snake_case``.

    Config keys are written with dashes (TOML idiom) but map straight onto the
    snake_case :class:`Config` fields, so coerce once after the read.
    """
    if isinstance(obj, dict):
        return {k.replace("-", "_"): _snake_keys(v) for k, v in obj.items()}
    return obj


def _parse_str_list(raw: Any, key: str, default: list[str]) -> list[str]:
    """Validate a string-list config field; a bare string means one entry.

    `list("benchmarks")` would silently split into characters, so the string
    case must be handled before the list case.
    """
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    raise ValueError(f"[tool.mew] {key} must be a string or a list of strings")


def load(start: Path | None = None) -> Config:
    """Read ``[tool.mew]`` from the nearest ``pyproject.toml``.

    Parameters
    ----------
    start : Path, optional
        Directory to start the upward search from; defaults to the cwd.

    Returns
    -------
    Config
        Settings from the first ``pyproject.toml`` found walking upward, or
        all-default settings if there is none. The first file found wins even
        when it has no ``[tool.mew]`` table.

    Raises
    ------
    ValueError
        If a config field has the wrong shape.
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        tool = _snake_keys(data.get("tool", {}).get("mew", {}))
        statistic = tool.get("statistic")
        if statistic is not None and not isinstance(statistic, str):
            raise ValueError("[tool.mew] statistic must be a string")
        setup = tool.get("setup")
        if setup is not None and not isinstance(setup, str):
            raise ValueError("[tool.mew] setup must be a string")
        return Config(
            benchpaths=_parse_str_list(tool.get("benchpaths"), "benchpaths", ["benchmarks"]),
            python_files=_parse_str_list(
                tool.get("python_files"), "python-files", ["bench_*.py", "*_bench.py"]
            ),
            setup=setup,
            statistic=statistic,
            project_root=parent,
        )
    return Config()
