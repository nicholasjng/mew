"""Compare benchmark samples and render their differences."""

from __future__ import annotations

import functools
import json
import math
import re
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mew._console import Span, Table, Terminal, fill, overflow, sgr
from mew._registry import compile_name_filter
from mew._results import (
    _MEMORY_METRICS,
    _METRICS,
    _TIME_METRICS,
    Sample,
    SessionSummary,
    _load,
    _read_rows,
    _split_selector,
    read_results,
    session_summaries,
)
from mew._significance import mannwhitney_p
from mew._statistics import Statistic
from mew.regressions import BenchmarkVerdict, RegressionConfig, report
from mew.reporter import _NS_PER_UNIT, _fmt_bytes, _fmt_time, _scale_time, _to_ns

__all__ = ["Sample", "SessionSummary", "compare", "read_results", "session_summaries"]

_HIGHER_IS_BETTER = frozenset({"iterations"})
_KEYS = frozenset({"name", "func"})
# Coefficient of variation above which measurements are flagged as noisy.
_CV_UNRELIABLE = 0.25
_CTX_SKEW_FIELDS = ("cpu_model", "num_cpus", "cpu_scaling_enabled")


def _label(path: Path, others: list[Path]) -> str:
    stem = path.stem
    if sum(1 for p in others if p.stem == stem) > 1:
        return str(path)
    return stem


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested user-context values to dotted keys for display."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{dotted}."))
        else:
            out[dotted] = v
    return out


def _ctx_summary(ctx: dict[str, Any], *, exclude: Collection[str]) -> list[str]:
    """The ``key=value`` parts of a comparison column's provenance line.

    ``exclude`` omits user-context keys already shown in the column label.
    """
    sess = ctx.get("session") or {}
    provenance = ctx.get("context") or {}
    parts: list[str] = []
    if sess.get("tag"):
        parts.append(f"session={sess['tag']}")
    elif sess.get("id"):
        parts.append(f"session={str(sess['id'])[:12]}")
    if sess.get("host"):
        parts.append(f"host={sess['host']}")
    if sess.get("date"):
        parts.append(f"date={str(sess['date'])[:19]}")
    for k, v in _flatten(provenance).items():
        if k in exclude:
            continue
        parts.append(f"{k}={v}")
    return parts


def _warn_context_skew(columns: list[_Column]) -> None:
    """Warn when machine-level context differs across columns (deltas then compare
    environments, not just code)."""
    # `host` lives in the session block, the rest in provenance; all warn alike.
    for block, fld in (("session", "host"), *(("context", f) for f in _CTX_SKEW_FIELDS)):
        values = {c.label: (c.context.get(block) or {}).get(fld) for c in columns if c.context}
        if len({v for v in values.values() if v is not None}) > 1:
            detail = ", ".join(f"{label}: {v}" for label, v in values.items())
            print(
                f"warning: result files differ in {fld} ({detail}); "
                "deltas may reflect the environment, not the code",
                file=sys.stderr,
            )


def _custom_diffs(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-column dict of the context keys whose values differ across columns."""
    flats = [_flatten(ctx.get("context") or {}) for ctx in contexts]
    all_keys: set[str] = set().union(*flats)
    differing = sorted(
        k
        for k in all_keys
        if len({json.dumps(f.get(k), sort_keys=True, default=str) for f in flats}) > 1
    )
    return [{k: f.get(k) for k in differing if k in f} for f in flats]


def _fmt_value(sample: Sample, metric: str) -> str:
    if metric == "memory.allocations_per_iteration":  # fractional per-call count
        return f"{sample.value:,.1f}"
    if metric == "iterations":
        return f"{int(sample.value):,}"
    if metric in _MEMORY_METRICS:  # remaining memory metrics are byte-valued
        return _fmt_bytes(int(sample.value))
    return _fmt_time(sample.value, sample.time_unit)


def _fmt_stddev(sample: Sample, metric: str) -> str:
    """Stddev cell, scaled to the same unit as its paired value cell."""
    if sample.stddev is None:
        return "-"
    if metric == "memory.peak_bytes":
        return _fmt_bytes(int(sample.stddev))
    if metric not in _TIME_METRICS:
        return f"{sample.stddev:.2f}"
    _, unit = _scale_time(sample.value, sample.time_unit)
    scaled = _to_ns(sample.stddev, sample.time_unit) / _NS_PER_UNIT[unit]
    return f"{scaled:.2f} {unit}"


# p-value threshold for the Mann-Whitney-U test.
_SIGNIFICANCE_ALPHA = 0.05


@dataclass(frozen=True, slots=True)
class _Comparison:
    delta: float
    speedup: float
    p_value: float | None


def _compare_samples(base: Sample, other: Sample, metric: str) -> _Comparison:
    """Calculate a comparison independently of table layout and gating policy."""
    is_time = metric in _TIME_METRICS
    base_value = _to_ns(base.value, base.time_unit) if is_time else base.value
    other_value = _to_ns(other.value, other.time_unit) if is_time else other.value
    if base_value:
        delta = (other_value - base_value) / base_value
    else:
        # A zero baseline must not mask a nonzero contender as unchanged.
        delta = 0.0 if not other_value else float("inf")
    num, den = (
        (other_value, base_value) if metric in _HIGHER_IS_BETTER else (base_value, other_value)
    )
    # Mann-Whitney over raw repetitions; fewer than 2 per side leaves nothing to rank.
    p_value: float | None = None
    if len(base.values) >= 2 and len(other.values) >= 2:
        a = [_to_ns(v, base.time_unit) if is_time else v for v in base.values]
        b = [_to_ns(v, other.time_unit) if is_time else v for v in other.values]
        p_value = mannwhitney_p(a, b)
    return _Comparison(
        delta=delta,
        # 0 vs 0 (e.g. allocation-free bodies) is unchanged, not infinitely faster.
        speedup=num / den if den else (1.0 if not num else math.inf),
        p_value=p_value,
    )


def _fmt_delta(delta: float, *, higher_is_better: bool = False) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.2f}%" if math.isfinite(pct) else "+∞%"
    # Color by improvement direction: +20% iterations is green, +20% time red.
    worse = -delta if higher_is_better else delta
    style = "green" if worse < 0 else "red" if worse > 0 else ""
    return text, style


def _value_cell(sample: Sample, metric: str) -> str | list[Span]:
    """The value text, with a red ``±N% (!)`` marker when repetitions scatter too much."""
    value = _fmt_value(sample, metric)
    cv = sample.cv
    if cv is None or cv < _CV_UNRELIABLE:
        return value
    return [(value, None), (f" ±{cv * 100.0:.0f}% (!)", "red")]


@dataclass(slots=True)
class _Column:
    """One comparison column: a result file, or one ``path@selector`` session of it.

    ``source`` identifies the column in warnings (``path`` or ``path@selector``);
    ``label`` heads its table column.
    """

    source: str
    label: str
    samples: dict[str, Sample]
    context: dict[str, Any]


def _render(
    columns: list[_Column],
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    show_stddev: bool,
    regressions: RegressionConfig | None,
    console: Terminal | None,
    key: str = "name",
) -> int:
    """Compare the first column against the rest and render the table.

    Column-shaped on purpose: anything producing labelled sample sets with contexts
    (files, ``path@selector`` sessions) compares the same way.
    """
    all_names: set[str] = set().union(*(c.samples.keys() for c in columns))
    if pattern is not None:
        all_names = {n for n in all_names if pattern.search(n)}

    # The rows are what baseline and candidate share, gated or not: a benchmark
    # missing from an extra column, such as an older run, shows dashes there.
    shared = all_names.intersection(columns[0].samples, columns[1].samples)
    if not shared:
        msg = (
            f"no overlapping benchmarks between {columns[1].source} "
            f"and the baseline {columns[0].source}"
        )
        if metric in _MEMORY_METRICS:
            msg += f" with {metric!r} data (produced with --profile-memory?)"
        elif key == "name":
            msg += (
                " (suites with matching function names in different files overlap with --key func)"
            )
        print(msg, file=sys.stderr)
        return 1

    for c in columns:
        missing = all_names - c.samples.keys()
        if missing:
            preview = ", ".join(sorted(missing)[:5])
            extra = overflow(len(missing), 5)
            print(
                f"warning: {c.source} missing {len(missing)} benchmark(s): {preview}{extra}",
                file=sys.stderr,
            )

    _warn_context_skew(columns)
    # Differing custom-context keys (e.g. engine=...) go on the context line, not
    # the table header, to save width.
    diffs = _custom_diffs([c.context for c in columns])
    annotated_labels = [
        f"{c.label} ({', '.join(f'{k}={v}' for k, v in diff.items())})" if diff else c.label
        for c, diff in zip(columns, diffs, strict=True)
    ]

    term = console or Terminal()
    for label, c, diff in zip(annotated_labels, columns, diffs, strict=True):
        if c.context:
            parts = [f"{label}:", *_ctx_summary(c.context, exclude=diff.keys())]
            for line in fill(parts, term.width, indent="", hang="  ", sep=" "):
                term.print(sgr(line, "dim", enabled=term.color))

    labels = [c.label for c in columns]
    table = Table(title=f"Comparison ({metric}, baseline {labels[0]})")
    table.add_column("Benchmark", flex=True)
    table.add_column(labels[0], justify="right")
    if show_stddev:
        table.add_column("± stddev", justify="right")
    for lbl in labels[1:]:
        table.add_column(lbl, justify="right")
        table.add_column("Δ%", justify="right")
        # Memory: less isn't "faster".
        table.add_column("ratio" if metric in _MEMORY_METRICS else "speedup", justify="right")
        if show_stddev:
            table.add_column("± stddev", justify="right")

    higher_is_better = metric in _HIGHER_IS_BETTER
    is_time_metric = metric in _TIME_METRICS
    baseline = columns[0].samples
    verdicts: list[BenchmarkVerdict] = []
    unit_skew: dict[str, tuple[Any, Any]] = {}

    for name in sorted(shared):
        base = baseline[name]
        row: list[Any] = [name, _value_cell(base, metric)]
        if show_stddev:
            row.append(_fmt_stddev(base, metric))
        for idx, c in enumerate(columns[1:]):
            if name not in c.samples:
                row.extend(["-"] * (4 if show_stddev else 3))
                continue
            s = c.samples[name]
            if is_time_metric and base.time_unit != s.time_unit:
                unit_skew[name] = (base.time_unit, s.time_unit)
            result = _compare_samples(base, s, metric)
            if idx == 0 and regressions is not None:
                verdicts.append(
                    regressions.evaluate(
                        name, result.delta * 100.0, higher_is_better=higher_is_better
                    )
                )
            delta_cell: list[Span] = [_fmt_delta(result.delta, higher_is_better=higher_is_better)]
            if result.p_value is not None and result.p_value < _SIGNIFICANCE_ALPHA:
                delta_cell.append((" (signif.)", "bold"))
            row.append(_value_cell(s, metric))
            row.append(delta_cell)
            row.append(f"×{result.speedup:.3f}")
            if show_stddev:
                row.append(_fmt_stddev(s, metric))
        table.add_row(*row)

    if unit_skew:
        name, (a, b) = next(iter(unit_skew.items()))
        extra = overflow(len(unit_skew), 1)
        print(
            f"note: {len(unit_skew)} benchmark(s) declare different time units "
            f"across files (e.g. {name!r}: {a!r} vs {b!r}){extra}; values are "
            "normalized to a common unit before comparing",
            file=sys.stderr,
        )

    term.print(table)

    if regressions is not None:
        return report(verdicts, default_threshold=regressions.default_threshold)
    return 0


def compare(
    files: list[Path],
    *,
    metric: str = "real_time",
    key: str = "name",
    pattern: str | None = None,
    literal: bool = False,
    show_stddev: bool = False,
    statistic: Statistic | None = None,
    regressions: RegressionConfig | None = None,
    console: Terminal | None = None,
) -> int:
    """Print a table comparing result files against the last, the baseline.

    Each other file's column shows its value, the delta and the speedup. The
    rows are the benchmarks the first file and the baseline share; another file
    that lacks one shows dashes in its row.

    Parameters
    ----------
    files : list[Path]
        JSON, JSONL or gzipped JSONL result files, the baseline last:
        ``mew compare head.json base.json`` compares head against base. A file
        with several sessions contributes its newest; ``path@<tag>`` selects
        the sessions run with that ``--session-tag``, and ``path@latest`` the
        newest explicitly (``latest`` is reserved, never a tag). A path splits
        at its last ``@`` only after a ``.json``, ``.jsonl`` or ``.jsonl.gz``
        extension, so ``a@b.json`` is a file name.
    metric : str, default "real_time"
        ``"real_time"``, ``"cpu_time"``, ``"iterations"``, or, for results with
        memory figures, ``"memory.peak_bytes"`` or
        ``"memory.allocations_per_iteration"``; the latter compares
        implementations of different speed.
    key : str, default "name"
        How rows match across files: ``"name"`` by the full name, ``"func"``
        without the ``file.py::`` prefix, so A/B suites in different files line up.
    pattern : str, optional
        Regex searched for in benchmark names.
    literal : bool, default False
        Match ``pattern`` as plain text, e.g. to keep a ``name[label]``'s brackets.
    show_stddev : bool, default False
        Add a stddev column per file, where results have repetitions.
    statistic : Callable[[list[float]], SupportsFloat], optional
        Reduces each benchmark's repetition values to the value shown and
        gated; the median by default. Stddev is unaffected.
    regressions : RegressionConfig, optional
        Gate the first file against the baseline, and print a regression panel.
    console : Terminal, optional
        Where to print; defaults to stdout.

    Returns
    -------
    int
        ``0``; ``1`` when the first file and the baseline share no benchmark;
        ``3`` when the regression gate fails.

    Raises
    ------
    ValueError
        If an argument, such as ``pattern``, is invalid, or a file can't be
        compared: it is not a mew result file, holds no measurements or no
        session with the given tag, or ``statistic`` fails on a benchmark's
        values.
    OSError
        If a file can't be read; :class:`FileNotFoundError` if it doesn't exist.
    """
    if metric not in _METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    if key not in _KEYS:
        raise ValueError(f"unknown key {key!r}; choose from {sorted(_KEYS)}")
    name_filter = compile_name_filter(pattern, literal=literal) if pattern else None

    if len(files) < 2:
        raise ValueError(f"expected at least two result files, got {len(files)}")
    parsed = [_split_selector(str(p)) for p in files]
    paths = [p for p, _ in parsed]
    # CLI convention: the last file is the baseline ("compare head against
    # baseline"), while `_render` expects columns[0] to be the baseline.
    ordered = [parsed[-1], *parsed[:-1]]
    # Two selectors on one archive must not parse it twice.
    read_once = functools.cache(_read_rows)

    columns = []
    for path, selector in ordered:
        samples, ctx = _load(path, metric, key, selector, statistic, reader=read_once)
        base = _label(path, paths)
        label = f"{base}@{selector}" if selector else base
        columns.append(
            _Column(
                source=f"{path}@{selector}" if selector else str(path),
                label=label,
                samples=samples,
                context=ctx,
            )
        )
    return _render(
        columns,
        metric=metric,
        pattern=name_filter,
        show_stddev=show_stddev,
        regressions=regressions,
        console=console,
        key=key,
    )
