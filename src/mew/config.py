"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `mew run --capture` values; also accepted as `[tool.mew] capture`.
CAPTURE_MODES = ("fd", "no")


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
        If a config field has the wrong shape.
    """
    import tomllib

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            data = tomllib.load(fh)
        # Keys are written with dashes (TOML idiom) but map onto snake_case fields.
        tool = {k.replace("-", "_"): v for k, v in data.get("tool", {}).get("mew", {}).items()}
        cfg = Config(
            statistic=_parse_str(tool, "statistic"),
            setup=_parse_str(tool, "setup"),
            capture=_parse_str(tool, "capture"),
            project_root=parent,
        )
        if cfg.capture not in (None, *CAPTURE_MODES):
            raise ValueError(f"[tool.mew] capture must be one of {', '.join(CAPTURE_MODES)}")
        if (raw := tool.get("benchpaths")) is not None:
            cfg.benchpaths = _parse_str_list(raw, "benchpaths")
        if (raw := tool.get("python_files")) is not None:
            cfg.python_files = _parse_str_list(raw, "python-files")
        return cfg
    return Config()
