"""Resolve ``[tool.mew]`` config from the nearest ``pyproject.toml``."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mew._statistics import resolve_statistic
from mew.regressions import AllowRule, RegressionConfig

#: The ``mew run --capture`` values, also accepted as ``[tool.mew] capture``.
CAPTURE_MODES = ("fd", "no")

# `[tool.mew]` keys.
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
    regressions : RegressionConfig
        The ``[tool.mew.regressions]`` gate: default threshold and allowlist rules.
    project_root : Path or None
        Directory of the ``pyproject.toml`` these settings came from; ``None``
        when no file was found and defaults are in use.
    """

    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    setup: str | None = None
    statistic: str | None = None
    capture: str | None = None
    regressions: RegressionConfig = field(default_factory=RegressionConfig)
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
        If the file is not valid TOML, or a key is unknown, such as a
        snake_case spelling of a dashed key, or has an invalid value. The
        message starts with the file's path.
    """
    import tomllib

    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            try:  # A TOML syntax error is a ValueError too.
                with candidate.open("rb") as fh:
                    raw = tomllib.load(fh).get("tool", {}).get("mew", {})
                return _parse_config(raw, project_root=parent)
            except ValueError as e:
                raise ValueError(f"{candidate}: {e}") from e
    return Config()


def _parse_config(raw: Mapping[str, Any], *, project_root: Path) -> Config:
    """Validate a raw ``[tool.mew]`` table into a :class:`Config`."""
    tool = _read_table(raw, _KEYS, "[tool.mew]")
    cfg = Config(
        statistic=_parse_str(tool, "statistic"),
        setup=_parse_str(tool, "setup"),
        capture=_parse_str(tool, "capture"),
        regressions=_parse_regressions(tool.get("regressions", {})),
        project_root=project_root,
    )
    if cfg.capture not in (None, *CAPTURE_MODES):
        raise ValueError(f"[tool.mew] capture must be one of {', '.join(CAPTURE_MODES)}")
    if cfg.statistic is not None:
        # Fail at load, not at the first `mew compare`.
        try:
            resolve_statistic(cfg.statistic)
        except ValueError as e:
            raise ValueError(f"[tool.mew] {e}") from e
    if (value := tool.get("benchpaths")) is not None:
        cfg.benchpaths = _parse_str_list(value, "benchpaths")
    if (value := tool.get("python_files")) is not None:
        cfg.python_files = _parse_str_list(value, "python-files")
    return cfg


def _parse_regressions(raw: Any) -> RegressionConfig:
    """Validate the ``[tool.mew.regressions]`` table into a :class:`RegressionConfig`."""
    if not isinstance(raw, dict):
        raise ValueError("[tool.mew.regressions] must be a table")
    table = _read_table(raw, ("default-threshold", "allow"), "[tool.mew.regressions]")
    allow = table.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(r, dict) for r in allow):
        raise ValueError("[tool.mew.regressions] allow must be an array of tables")
    rules = tuple(_parse_allow_rule(rule) for rule in allow)
    # TOML has no null, so None means the key is absent.
    if (threshold := table.get("default_threshold")) is None:
        return RegressionConfig(rules=rules)
    return RegressionConfig(default_threshold=threshold, rules=rules)


def _parse_allow_rule(raw: Mapping[str, Any]) -> AllowRule:
    """Validate one ``[[tool.mew.regressions.allow]]`` table into an :class:`AllowRule`."""
    rule = _read_table(
        raw, ("pattern", "reason", "ignore", "threshold"), "[[tool.mew.regressions.allow]]"
    )
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("allow rule missing 'pattern'")

    reason = rule.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"allow rule {pattern!r}: 'reason' is required (document why this is allowlisted)"
        )

    try:
        return AllowRule(
            pattern=pattern,
            reason=reason.strip(),
            ignore=cast(bool, rule.get("ignore", False)),
            threshold=cast(float | None, rule.get("threshold")),
        )
    except (TypeError, ValueError) as e:
        # The model owns validation; the loader adds configuration context.
        raise ValueError(f"allow rule {pattern!r}: {e}") from e
