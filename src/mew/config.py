"""Resolve `[tool.mew]` config from the nearest pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    """Resolved ``[tool.mew]`` settings.

    Attributes
    ----------
    benchpaths : list[str]
        Directories searched when no path argument is given, relative to
        ``project_root``.
    python_files : list[str]
        Glob patterns identifying benchmark files during discovery.
    session_tag : SessionTagSpec
        Whether and how to derive the automatic session tag.
    statistic : str or None
        Default ``mew compare`` reducer; ``None`` keeps the median.
    project_root : Path or None
        Directory of the ``pyproject.toml`` these settings came from; ``None``
        when no file was found and defaults are in use.
    """

    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    session_tag: SessionTagSpec = field(default_factory=SessionTagSpec)
    # Default `mew compare` reducer: a built-in name or "module.path:attr" ref
    # (see _statistics.resolve_statistic); None keeps the median. --statistic wins.
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


def _parse_session_tag(raw: Any) -> SessionTagSpec:
    if not isinstance(raw, dict):
        raise TypeError("[tool.mew.session-tag] must be a table")
    keys: set[str] = {str(k) for k in raw}
    if unknown := keys - {"enabled", "tool", "args"}:
        raise ValueError(f"unknown keys in [tool.mew.session-tag]: {sorted(unknown)}")
    enabled, tool, args = raw.get("enabled", True), raw.get("tool"), raw.get("args")
    if not isinstance(enabled, bool):
        raise TypeError("[tool.mew.session-tag] enabled must be a boolean")
    if tool is not None and not isinstance(tool, str):
        raise TypeError("[tool.mew.session-tag] tool must be a string")
    if args is None:
        return SessionTagSpec(enabled=enabled, tool=tool, args=None)
    if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
        raise ValueError("[tool.mew.session-tag] args must be a list of strings")
    return SessionTagSpec(enabled=enabled, tool=tool, args=[str(a) for a in args])


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
    TypeError
        If ``[tool.mew.session-tag]`` is not a table, or one of its fields has
        the wrong type.
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
            raise ValueError("[tool.mew] statistic must be a 'module.path:attr' string")
        return Config(
            benchpaths=_parse_str_list(tool.get("benchpaths"), "benchpaths", ["benchmarks"]),
            python_files=_parse_str_list(
                tool.get("python_files"), "python-files", ["bench_*.py", "*_bench.py"]
            ),
            session_tag=_parse_session_tag(tool.get("session_tag", {})),
            statistic=statistic,
            project_root=parent,
        )
    return Config()
