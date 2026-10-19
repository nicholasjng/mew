"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mew._statistics import resolve_statistic

# `mew run --capture` values; also accepted as `[tool.mew] capture`.
CAPTURE_MODES = ("fd", "no")

# `[tool.mew]` keys; `regressions` is the table that `mew.regressions.load_config` reads.
_KEYS = ("benchpaths", "python-files", "setup", "statistic", "capture", "regressions")


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
        A Python file, relative to ``project_root``, imported before any
        benchmark file: the place for context every run should record, whichever
        benchmarks it selects.
    statistic : str or None
        Default ``mew compare`` reducer; ``None`` keeps the median.
    capture : str or None
        Default for ``mew run --capture``: ``"fd"`` captures benchmark output and
        prints it after the run, ``"no"`` shows it live; ``None`` keeps ``"fd"``.
    project_root : Path or None
        Directory of the ``pyproject.toml`` these settings came from; ``None``
        when no file was found and defaults are in use.
    """

    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    setup: str | None = None
    statistic: str | None = None
    capture: str | None = None
    project_root: Path | None = None


def _read_table(raw: Mapping[str, Any], known: Sequence[str], table: str) -> dict[str, Any]:
    """Return a config table with its dashed keys (the TOML idiom) in snake_case.

    A key not in ``known`` raises, because a typo would otherwise load the
    default silently; a snake_case spelling of a dashed key is one too.
    """
    for key in raw:
        if key not in known:
            close = difflib.get_close_matches(key, known, n=1)
            hint = f"; did you mean {close[0]!r}?" if close else ""
            raise ValueError(f"unknown key {key!r} in {table}{hint}")
    return {key.replace("-", "_"): value for key, value in raw.items()}


def _parse_str_list(raw: Any, key: str) -> list[str]:
    """Validate a string-list config field; a bare string means one entry.

    `list("benchmarks")` would silently split into characters, so the string
    case must be handled before the list case.
    """
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    raise ValueError(f"[tool.mew] {key} must be a string or a list of strings")


def _parse_str(tool: dict[str, Any], key: str) -> str | None:
    """Validate an optional string config field."""
    value = tool.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"[tool.mew] {key} must be a string")
    return value


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
        If a key is unknown, such as a snake_case spelling of a dashed key, or
        has an invalid value.
    """
    import tomllib

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        tool = _read_table(data.get("tool", {}).get("mew", {}), _KEYS, "[tool.mew]")
        cfg = Config(
            statistic=_parse_str(tool, "statistic"),
            setup=_parse_str(tool, "setup"),
            capture=_parse_str(tool, "capture"),
            project_root=parent,
        )
        if cfg.capture not in (None, *CAPTURE_MODES):
            raise ValueError(f"[tool.mew] capture must be one of {', '.join(CAPTURE_MODES)}")
        if cfg.statistic is not None:
            # Fail at load, not at the first `mew compare`.
            try:
                resolve_statistic(cfg.statistic)
            except ValueError as e:
                raise ValueError(f"[tool.mew] {e}") from e
        if (raw := tool.get("benchpaths")) is not None:
            cfg.benchpaths = _parse_str_list(raw, "benchpaths")
        if (raw := tool.get("python_files")) is not None:
            cfg.python_files = _parse_str_list(raw, "python-files")
        return cfg
    return Config()
