"""Compare benchmark samples and render their differences."""

from __future__ import annotations

import json
import math
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mew._console import Span, Table, Terminal, overflow, sgr
from mew._registry import compile_name_filter
from mew._results import (
    _MEMORY_METRICS,
    _METRICS,
    _NS_PER_UNIT,
    _TIME_METRICS,
    Sample,
    SessionData,
    _load,
    _load_pivot_columns,
    _split_selector,
    _to_ns,
    read_results,
    read_sessions,
)
from mew._significance import mannwhitney_p
from mew._statistics import Statistic
from mew.regressions import RegressionConfig, report
from mew.reporter import _fmt_bytes

__all__ = ["Sample", "SessionData", "compare", "read_results", "read_sessions"]

_HIGHER_IS_BETTER = frozenset({"iterations"})
_KEYS = frozenset({"name", "func"})
# Coefficient of variation above which measurements are flagged as noisy.
_CV_UNRELIABLE = 0.25
_CTX_SKEW_FIELDS = ("num_cpus", "cpu_scaling_enabled")


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


def _ctx_summary(ctx: dict[str, Any], *, exclude: Iterable[str] = ()) -> str:
    """Build one provenance line per comparison column.

    ``exclude`` omits user-context keys already shown in the column label.
    """
    exclude = set(exclude)
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
    return " ".join(parts)


def _warn_context_skew(columns: list[_Column]) -> None:
    """Warn when machine-level context differs across columns (deltas then compare
    environments, not just code)."""
    # `host` lives in the session block (grouping and ordering key on it), the
    # rest in provenance -- but a mismatch in any of them makes the comparison
    # suspect, so they warn the same way.
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


def _scale_time(value: float, unit: str | None) -> tuple[float, str]:
    """Rescale a raw ``(value, unit)`` pair to whichever of s/ms/us/ns keeps the
    mantissa >= 1, e.g. ``(6995135790.99, "ns")`` -> ``(7.00, "s")``. Display-only.
    """
    ns = _to_ns(value, unit)
    for threshold, out_unit in ((1e9, "s"), (1e6, "ms"), (1e3, "us")):
        if abs(ns) >= threshold:
            return ns / threshold, out_unit
    return ns, "ns"


def _fmt_value(sample: Sample, metric: str) -> str:
    if metric == "memory.allocations_per_iteration":  # fractional per-call count
        return f"{sample.value:,.1f}"
    if metric == "iterations":
        return f"{int(sample.value):,}"
    if metric in _MEMORY_METRICS:  # remaining memory metrics are byte-valued
        return _fmt_bytes(int(sample.value))
    scaled, unit = _scale_time(sample.value, sample.time_unit)
    return f"{scaled:.2f} {unit}"


def _fmt_stddev(sample: Sample, metric: str) -> str:
    """Stddev cell, scaled by the same unit as its paired value cell so the two
    stay comparable at a glance instead of showing raw ns next to human-scaled s."""
    if sample.stddev is None:
        return "-"
    if metric not in _TIME_METRICS:
        return f"{sample.stddev:.2f}"
    _, unit = _scale_time(sample.value, sample.time_unit)
    scaled = _to_ns(sample.stddev, sample.time_unit) / _NS_PER_UNIT[unit]
    return f"{scaled:.2f} {unit}"


# p-value threshold for the Mann-Whitney-U test.
_SIGNIFICANCE_ALPHA = 0.05


def _significance_p(base: Sample, other: Sample, *, is_time_metric: bool) -> float | None:
    """Mann-Whitney two-sided p-value between two samples' raw repetitions.

    ``None`` when either side has fewer than 2 repetitions (nothing to rank);
    no marker is shown then, same gating as the CV marker.
    """
    if len(base.values) < 2 or len(other.values) < 2:
        return None
    if is_time_metric:
        a = [_to_ns(v, base.time_unit) for v in base.values]
        b = [_to_ns(v, other.time_unit) for v in other.values]
    else:
        a, b = list(base.values), list(other.values)
    return mannwhitney_p(a, b)


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
    return _Comparison(
        delta=delta,
        speedup=num / den if den else float("inf"),
        p_value=_significance_p(base, other, is_time_metric=is_time),
    )


def _fmt_delta(delta: float, *, higher_is_better: bool = False) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.2f}%" if math.isfinite(pct) else "+∞%"
    # Color by improvement direction: +20% iterations is green, +20% time red.
    worse = -delta if higher_is_better else delta
    style = "green" if worse < 0 else "red" if worse > 0 else ""
    return text, style


def _fmt_speedup(speedup: float) -> str:
    return f"×{speedup:.3f}"


def _ratio_header(metric: str) -> str:
    """Header for the baseline/candidate ratio column ("speedup" for time, "ratio"
    for memory, where less isn't "faster")."""
    return "ratio" if metric in _MEMORY_METRICS else "speedup"


def _value_cell(sample: Sample, metric: str) -> str | list[Span]:
    """The value text, with a red ``±N% (!)`` marker when repetitions scatter too much."""
    value = _fmt_value(sample, metric)
    cv = sample.cv
    if cv is None or cv < _CV_UNRELIABLE:
        return value
    return [(value, None), (f" ±{cv * 100.0:.0f}% (!)", "red")]


@dataclass(slots=True)
class _Column:
    """One comparison column: a result file, or one value of a pivot dimension.

    ``source`` identifies the column in warnings (file path / pivot value);
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
    (files, sessions, pivot groups) compares the same way.
    """
    all_names: set[str] = set().union(*(c.samples.keys() for c in columns))
    if pattern is not None:
        all_names = {n for n in all_names if pattern.search(n)}

    # Informational columns must not remove candidate/baseline measurements
    # from the gate. Render absent historical values as dashes below.
    required = columns[:2] if regressions is not None else columns
    shared = all_names.intersection(*(c.samples.keys() for c in required))
    if not shared:
        msg = "no overlapping benchmarks across input files"
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
    # Custom-context keys that differ (e.g. engine=...) annotate the per-column
    # context line, so an apples-vs-oranges comparison documents itself without
    # stealing table-header width.
    diffs = _custom_diffs([c.context for c in columns])
    annotated_labels = [
        f"{c.label} ({', '.join(f'{k}={v}' for k, v in diff.items())})" if diff else c.label
        for c, diff in zip(columns, diffs, strict=True)
    ]

    term = console or Terminal()
    for label, c, diff in zip(annotated_labels, columns, diffs, strict=True):
        if c.context:
            summary = _ctx_summary(c.context, exclude=diff.keys())
            term.print(sgr(f"{label}: {summary}", "dim", enabled=term.color))

    labels = [c.label for c in columns]
    table = Table(title=f"Comparison ({metric})")
    table.add_column("Benchmark", flex=True)
    table.add_column(f"{labels[0]} (baseline)", justify="right")
    if show_stddev:
        table.add_column("± stddev", justify="right")
    for lbl in labels[1:]:
        table.add_column(lbl, justify="right")
        table.add_column("Δ%", justify="right")
        table.add_column(_ratio_header(metric), justify="right")
        if show_stddev:
            table.add_column("± stddev", justify="right")

    higher_is_better = metric in _HIGHER_IS_BETTER
    is_time_metric = metric in _TIME_METRICS
    baseline = columns[0].samples
    comparisons = [
        {
            name: _compare_samples(baseline[name], c.samples[name], metric)
            for name in sorted(all_names & baseline.keys() & c.samples.keys())
        }
        for c in columns[1:]
    ]
    # Gating uses candidate/baseline overlap, independently of rendered rows.
    verdicts = (
        [
            regressions.evaluate(name, result.delta * 100.0, higher_is_better=higher_is_better)
            for name, result in comparisons[0].items()
        ]
        if regressions is not None
        else []
    )
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
            result = comparisons[idx][name]
            delta_text, delta_style = _fmt_delta(result.delta, higher_is_better=higher_is_better)
            delta_cell: str | list[Span] = (
                [(delta_text, delta_style)] if delta_style else delta_text
            )
            if result.p_value is not None and result.p_value < _SIGNIFICANCE_ALPHA:
                spans = delta_cell if isinstance(delta_cell, list) else [(delta_cell, None)]
                delta_cell = [*spans, (" (signif.)", "bold")]
            row.append(_value_cell(s, metric))
            row.append(delta_cell)
            row.append(_fmt_speedup(result.speedup))
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


def _pivot_columns(
    path: Path,
    metric: str,
    key: str,
    dimension: str,
    baseline: str | None,
    statistic: Statistic | None = None,
) -> list[_Column]:
    """Build comparison columns by pivoting one file on ``dimension``, baseline first."""
    loaded = _load_pivot_columns(path, metric, key, dimension, statistic)
    names = [v for v, _, _ in loaded]
    if not names:
        raise SystemExit(
            f"{path}: no {dimension!r} data to pivot; set it per suite with "
            f"mew.set_context() and write both suites to this file"
        )
    if len(names) == 1:
        # The pivot runs inside one session, so a dimension that *defines* the
        # session (a commit, say) has one value here however many the file holds.
        raise SystemExit(
            f"{path}: --by {dimension} found only {names[0]!r} in the latest session; "
            f"a dimension that differs per session is addressed with selectors "
            f"instead, e.g. `mew compare {path}@latest {path}@~1`"
        )
    if baseline is not None and baseline not in names:
        raise SystemExit(f"{path}: --baseline {baseline!r} not among {dimension} values {names}")
    base = baseline or names[0]
    ordered = [base, *(n for n in names if n != base)]
    by_name = {v: (s, c) for v, s, c in loaded}
    return [
        _Column(source=f"{path}[{v}]", label=v, samples=by_name[v][0], context=by_name[v][1])
        for v in ordered
    ]


def compare(
    files: list[Path],
    *,
    metric: str = "real_time",
    key: str | None = None,
    pattern: str | None = None,
    literal: bool = False,
    show_stddev: bool = False,
    by: str | None = None,
    baseline: str | None = None,
    statistic: Statistic | None = None,
    regressions: RegressionConfig | None = None,
    console: Terminal | None = None,
) -> int:
    """Compare benchmark result files and render a comparison table.

    The last file is the baseline; earlier files show their value plus percent
    delta and speedup against it.

    Parameters
    ----------
    files : list[Path]
        Result files (JSON, JSONL, or JSONL.gz); the last is treated as the
        baseline (``mew compare head.json baseline.json`` reads like "compare
        head against baseline"). A ``path@selector`` argument picks one session
        from a multi-session file; see docs/guide/regressions.md for the
        selector grammar.
    metric : str, default "real_time"
        Metric to compare.
        One of ``"real_time"``, ``"cpu_time"``, ``"iterations"``, or (for files
        produced with ``--profile-memory``) ``"memory.peak_bytes"`` or
        ``"memory.allocations_per_iteration"`` (the per-call allocation count,
        comparable across engines regardless of speed).
    key : str, optional
        How benchmarks are matched across files: ``"name"`` uses the full
        registered name; ``"func"`` strips the ``file.py::`` prefix so suites
        in different files with matching function names line up (A/B suites).
        Defaults to ``"func"`` when ``by`` is set (each column's rows keep
        their own ``file.py::`` prefix, so the columns only line up on the
        function name) and ``"name"`` otherwise.
    pattern : str, optional
        Regex (``re.search``) filter applied to benchmark names.
    literal : bool, default False
        Match ``pattern`` as a literal string rather than a regex (e.g. to keep
        a ``name[label]``'s brackets literal).
    show_stddev : bool, default False
        Add per-file stddev columns when stddev data is present.
    by : str, optional
        Pivot dimension: compare values of one field *within* a single file, one
        column each, instead of comparing files. Typically ``"context.<key>"``, read
        from the per-suite values :func:`mew.set_context` records on every row.
    baseline : str, optional
        With ``by``, which value is the baseline column (default: the first one
        written).
    statistic : Callable[[list[float]], float], optional
        Reducer over each benchmark's per-repetition values, used as the displayed
        center and the regression-gate value (stddev is unaffected). Receives a
        ``list[float]`` and returns a float-castable scalar; defaults to
        ``statistics.median``. The CLI resolves ``--statistic`` to one of these via
        :func:`mew._statistics.resolve_statistic`.
    regressions : RegressionConfig, optional
        If given, gate the first file against the last (baseline) and append a regression panel.
    console : mew._console.Terminal, optional
        Output terminal; defaults to a fresh :class:`~mew._console.Terminal`.

    Returns
    -------
    int
        Exit code: ``0`` on success, ``1`` for no overlap, ``2`` if the regression gate fails.
    """
    if metric not in _METRICS:
        raise SystemExit(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    # Pivot columns share the file prefix, so they only align on the func name.
    if key is None:
        key = "func" if by else "name"
    if key not in _KEYS:
        raise SystemExit(f"unknown key {key!r}; choose from {sorted(_KEYS)}")
    try:
        name_filter = compile_name_filter(pattern, literal=literal) if pattern else None
    except ValueError as e:
        raise SystemExit(str(e)) from e

    if by is not None:
        if len(files) != 1:
            raise SystemExit(f"mew compare --by {by} takes exactly one result file")
        columns = _pivot_columns(files[0], metric, key, by, baseline, statistic)
    else:
        if baseline is not None:
            raise SystemExit("mew compare --baseline requires --by")
        if len(files) < 2:
            raise SystemExit("mew compare needs at least two result files")
        parsed = [_split_selector(str(p)) for p in files]
        paths = [p for p, _ in parsed]
        # CLI convention: the last file is the baseline ("compare head against
        # baseline"), while `_render` expects columns[0] to be the baseline.
        ordered = [parsed[-1], *parsed[:-1]]
        columns = []
        for path, selector in ordered:
            samples, ctx = _load(path, metric, key, selector, statistic)
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
