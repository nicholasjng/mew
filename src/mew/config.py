"""Resolve ``[tool.mew]`` config from the nearest ``pyproject.toml``."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mew._managers import Preset, parse_presets
from mew.regressions import AllowRule, CounterConfig, CounterRule, RegressionConfig

# `[tool.mew]` keys.
_KEYS = (
    "benchpaths",
    "python-files",
    "setup",
    "regressions",
    "counters",
    "profilers",
)


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
    regressions : RegressionConfig
        The ``[tool.mew.regressions]`` gate: default threshold and allowlist rules.
    counters : CounterConfig
        The ``[[tool.mew.counters]]`` rules: which direction is an improvement
        per counter.
    profilers : dict[str, Preset]
        The ``[tool.mew.profilers.<name>]`` presets ``mew run --cpu-profiler`` and
        ``--memory-profiler`` name.
    project_root : Path
        Directory of the ``pyproject.toml`` these settings came from, or the
        directory the search started in when there is none and defaults are
        in use. Paths in the settings resolve against it.
    """

    benchpaths: list[str] = field(default_factory=lambda: ["benchmarks"])
    python_files: list[str] = field(default_factory=lambda: ["bench_*.py", "*_bench.py"])
    setup: str | None = None
    regressions: RegressionConfig = field(default_factory=RegressionConfig)
    counters: CounterConfig = field(default_factory=CounterConfig)
    profilers: dict[str, Preset] = field(default_factory=dict)
    project_root: Path = field(default_factory=Path.cwd)


def _read_table(raw: Any, known: Sequence[str], table: str) -> dict[str, Any]:
    """Return a config table with its dashed keys (the TOML idiom) in snake_case.

    A key not in ``known`` raises, because a typo would otherwise load the
    default silently; a snake_case spelling of a dashed key is one too.
    """
    # E.g. `mew = []`, which would otherwise loop over nothing and load defaults.
    if not isinstance(raw, dict):
        raise ValueError(f"{table} must be a table")  # ruff: ignore[TRY004]
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
        if candidate.is_file():
            try:  # A TOML syntax error is a ValueError too.
                with candidate.open("rb") as fh:
                    tool = tomllib.load(fh).get("tool", {})
                if not isinstance(tool, dict):
                    raise ValueError("[tool] must be a table")  # ruff: ignore[TRY004]
                raw = tool.get("mew", {})
                return _parse_config(raw, project_root=parent)
            except ValueError as e:
                raise ValueError(f"{candidate}: {e}") from e
    return Config(project_root=cwd)


def _parse_config(raw: Mapping[str, Any], *, project_root: Path) -> Config:
    """Validate a raw ``[tool.mew]`` table into a :class:`Config`."""
    tool = _read_table(raw, _KEYS, "[tool.mew]")
    cfg = Config(
        setup=_parse_str(tool, "setup"),
        regressions=_parse_regressions(tool.get("regressions", {})),
        counters=_parse_counters(tool.get("counters", [])),
        profilers=parse_presets(tool.get("profilers", {})),
        project_root=project_root,
    )
    if (value := tool.get("benchpaths")) is not None:
        cfg.benchpaths = _parse_str_list(value, "benchpaths")
    if (value := tool.get("python_files")) is not None:
        cfg.python_files = _parse_str_list(value, "python-files")
    return cfg


def _parse_regressions(raw: Any) -> RegressionConfig:
    """Validate the ``[tool.mew.regressions]`` table into a :class:`RegressionConfig`."""
    table = _read_table(raw, ("default-threshold", "allow"), "[tool.mew.regressions]")
    allow = table.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(r, dict) for r in allow):
        raise ValueError("[tool.mew.regressions] allow must be an array of tables")
    rules = tuple(_parse_allow_rule(rule) for rule in allow)
    # TOML has no null, so None means the key is absent.
    if (threshold := table.get("default_threshold")) is None:
        return RegressionConfig(rules=rules)
    return RegressionConfig(default_threshold=threshold, rules=rules)


def _parse_counters(raw: Any) -> CounterConfig:
    """Validate the ``[[tool.mew.counters]]`` array into a :class:`CounterConfig`."""
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        raise ValueError("[tool.mew] counters must be an array of tables")
    return CounterConfig(rules=tuple(_parse_counter_rule(r) for r in raw))


def _parse_counter_rule(raw: Mapping[str, Any]) -> CounterRule:
    """Validate one ``[[tool.mew.counters]]`` table into a :class:`CounterRule`."""
    rule = _read_table(raw, ("pattern", "higher-is-better"), "[[tool.mew.counters]]")
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("counter rule missing 'pattern'")
    if "higher_is_better" not in rule:
        # A rule's purpose is the direction; a counter without one needs no rule.
        raise ValueError(f"counter rule {pattern!r}: 'higher-is-better' is required")
    try:
        return CounterRule(pattern, cast(bool, rule["higher_is_better"]))
    except (TypeError, ValueError) as e:
        raise ValueError(f"counter rule {pattern!r}: {e}") from e


def _parse_allow_rule(raw: Mapping[str, Any]) -> AllowRule:
    """Validate one ``[[tool.mew.regressions.allow]]`` table into an :class:`AllowRule`."""
    rule = _read_table(raw, ("pattern", "reason", "threshold"), "[[tool.mew.regressions.allow]]")
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("allow rule missing 'pattern'")

    reason = rule.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"allow rule {pattern!r}: 'reason' is required (document why this is allowlisted)"
        )

    if "threshold" not in rule:
        raise ValueError(
            f"allow rule {pattern!r}: 'threshold' is required, a percent or inf to exempt"
        )
    try:
        return AllowRule(
            pattern=pattern, reason=reason.strip(), threshold=cast(float, rule["threshold"])
        )
    except (TypeError, ValueError) as e:
        # The model owns validation; the loader adds configuration context.
        raise ValueError(f"allow rule {pattern!r}: {e}") from e
