"""Compare benchmark result files and print their differences."""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mew._console import (
    Span,
    Table,
    Terminal,
    error,
    fill,
    fmt_bytes,
    fmt_counter,
    fmt_time,
    overflow,
    sgr,
    warn,
)
from mew._registry import compile_name_filter
from mew._results import (
    MEMORY_METRICS,
    METRICS,
    SKEW_FIELDS,
    TIME_METRICS,
    RunSummary,
    Sample,
    counter_name,
    load_column,
    mannwhitney_p,
    read_results,
    run_summaries,
)
from mew.regressions import BenchmarkVerdict, CounterConfig, RegressionConfig, render_panel

__all__ = ["RunSummary", "compare", "read_results", "run_summaries"]

# Coefficient of variation above which measurements are flagged as noisy.
_CV_UNRELIABLE = 0.25
# `host` only where a suite records it; mew doesn't, since it often names a person.


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


def _ctx_summary(ctx: dict[str, Any], differing: dict[str, Any]) -> list[str]:
    """The parts of a comparison column's provenance line: its run, and the
    context values that differ from another column's. What every column shares
    goes unsaid. The skew warnings flag a machine that differs.

    An unnamed run goes by its date: a short id would not tell runs apart, as a
    UUIDv7's leading digits are its time to about a minute."""
    run = ctx.get("run") or {}
    parts: list[str] = []
    if (runs := ctx.get("runs", 1)) > 1:
        parts.append(f"{runs} runs, newest")
    elif run.get("name"):
        parts.append(f"run={run['name']}")
    if run.get("date"):
        parts.append(str(run["date"])[:19])
    parts += [f"{k}={v}" for k, v in differing.items()]
    return parts


def _warn_context_skew(columns: list[_Column]) -> None:
    """Warn when machine-level context differs across columns.

    The deltas then compare environments, not just code.
    """
    for fld in SKEW_FIELDS:
        values = {c.label: (c.context.get("context") or {}).get(fld) for c in columns if c.context}
        if len({v for v in values.values() if v is not None}) > 1:
            detail = ", ".join(f"{label}: {v}" for label, v in values.items())
            warn(f"result files differ in {fld}: {detail}")


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
    if counter_name(metric) is not None:
        return fmt_counter(sample.value, sample.counter_format)
    if metric == "memory.allocations_per_iteration":  # fractional per-call count
        return f"{sample.value:,.1f}"
    if metric in MEMORY_METRICS:  # remaining memory metrics are byte-valued
        return fmt_bytes(int(sample.value))
    return fmt_time(sample.value)


# p-value threshold for the Mann-Whitney-U test.
_SIGNIFICANCE_ALPHA = 0.05


@dataclass(frozen=True, slots=True)
class _Comparison:
    delta: float
    #: None where a ratio means nothing: a counter that is negative on either side.
    speedup: float | None
    p_value: float | None


def _compare_samples(base: Sample, other: Sample, higher_is_better: bool | None) -> _Comparison:
    """Calculate a comparison independently of table layout and gating policy.

    The ratio is the improvement factor where the metric's direction is known
    (``base / other`` for lower-is-better metrics), else ``other / base``. It is
    ``None`` when either value is negative: such a counter has no true zero, and
    a ratio would jump sign where the values cross zero. The delta stays.
    """
    base_value, other_value = base.value, other.value
    if base_value:
        # abs(): a counter can be negative, and -10 -> -5 is a rise, not a fall.
        delta = (other_value - base_value) / abs(base_value)
    else:
        # A zero baseline must not mask a nonzero contender as unchanged.
        delta = math.copysign(math.inf, other_value) if other_value else 0.0
    lower = higher_is_better is False
    num, den = (base_value, other_value) if lower else (other_value, base_value)
    # Mann-Whitney over raw repetitions; fewer than 2 per side leaves nothing to rank.
    p_value: float | None = None
    if len(base.values) >= 2 and len(other.values) >= 2:
        p_value = mannwhitney_p(list(base.values), list(other.values))
    if base_value < 0 or other_value < 0:
        speedup = None
    elif den:
        speedup = num / den
    else:
        # 0 vs 0 (e.g. allocation-free bodies) is unchanged, not infinitely faster.
        speedup = 1.0 if not num else math.inf
    return _Comparison(delta=delta, speedup=speedup, p_value=p_value)


def _fmt_delta(delta: float, *, higher_is_better: bool | None = False) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.2f}%" if math.isfinite(pct) else "+∞%" if pct > 0 else "-∞%"
    if higher_is_better is None:  # a counter without a direction: neither good nor bad
        return text, ""
    # Color by improvement direction: +20% throughput is green, +20% time red.
    worse = -delta if higher_is_better else delta
    style = "green" if worse < 0 else "red" if worse > 0 else ""
    return text, style


def _value_cell(sample: Sample, metric: str) -> str | list[Span]:
    """The value and its repetitions' spread, ``±N%`` of their mean: dim, or red with
    ``(!)`` when they scatter too much to trust the value."""
    value = _fmt_value(sample, metric)
    cv = sample.cv
    if cv is None:  # a single repetition has no spread
        return value
    pct = cv * 100.0
    spread = f" ±{pct:.1f}%" if pct < 10 else f" ±{pct:.0f}%"
    if cv < _CV_UNRELIABLE:
        return [(value, None), (spread, "dim")]
    return [(value, None), (spread + " (!)", "red")]


@dataclass(slots=True)
class _Column:
    """One comparison column: a result file, all of its runs as one sample.

    ``source``, its path, identifies the column in warnings. ``label`` heads its
    table column. ``skipped`` maps the benchmarks the file has only as skipped
    to their reasons.
    """

    source: str
    label: str
    samples: dict[str, Sample]
    context: dict[str, Any]
    skipped: dict[str, str] = field(default_factory=dict)


def _shared_file_prefix(names: Collection[str]) -> str:
    """The ``path.py::`` every name starts with, or ``""`` if they don't share one."""
    prefixes = {n.split("::", 1)[0] + "::" if "::" in n else "" for n in names}
    return prefixes.pop() if len(prefixes) == 1 else ""


def _summary(outcomes: list[tuple[int, bool | None]], *, metric: str, has_direction: bool) -> str:
    """One line on the first file against the baseline: what changed significantly."""
    if not outcomes:
        return "nothing to compare"
    if all(sig is None for _, sig in outcomes):
        return f"{len(outcomes)} compared, no significance test (needs 2+ repetitions a side)"
    changed = [d for d, sig in outcomes if sig]
    rest = len(outcomes) - len(changed)
    if not has_direction:
        return f"{len(changed)} changed significantly, {rest} not"
    worse, better = ("slower", "faster") if metric in TIME_METRICS else ("worse", "better")
    n_worse = sum(1 for d in changed if d > 0)
    return (
        f"{n_worse} {worse}, {len(changed) - n_worse} {better}, {rest} not significantly different"
    )


def _render(
    columns: list[_Column],
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    regressions: RegressionConfig | None,
    console: Terminal | None,
    higher_is_better: bool | None,
) -> int:
    """Compare the first column against the rest and render the table.

    Column-shaped on purpose: anything producing labelled sample sets with contexts
    (files, or anything else) compares the same way. ``higher_is_better``
    is the metric's direction. ``None`` leaves deltas uncolored and can't be gated.
    """
    counter = counter_name(metric)
    all_names: set[str] = set().union(*(c.samples.keys() for c in columns))
    if pattern is not None:
        all_names = {n for n in all_names if pattern.search(n)}
        if not all_names:
            error(f"no benchmarks match {pattern.pattern!r}")
            return 1

    # The rows are what baseline and candidate share, gated or not: a benchmark
    # missing from an extra column, such as an older run, shows dashes there.
    shared = all_names.intersection(columns[0].samples, columns[1].samples)
    if not shared:
        what = "benchmarks" if metric in TIME_METRICS else f"benchmarks with {metric}"
        msg = f"no {what} in both {columns[0].source} and {columns[1].source}"
        if metric in MEMORY_METRICS:
            msg += "\nhint: memory metrics need --memory-profiler memray"
        elif counter is not None:
            msg += "\nhint: benchmarks set counters with state.set_counter()"
        error(msg)
        return 1

    for c in columns:
        # A benchmark the run skipped is there, with a reason; a missing one isn't.
        skipped = sorted(all_names & c.skipped.keys() - c.samples.keys())
        if skipped:
            shown = ", ".join(f"{n} ({c.skipped[n]})" for n in skipped[:5])
            warn(f"skipped in {c.source}: {shown}{overflow(len(skipped), 5)}")
        missing = sorted(all_names - c.samples.keys() - c.skipped.keys())
        if missing:
            preview = ", ".join(missing[:5])
            extra = overflow(len(missing), 5)
            warn(f"{c.source}: missing {len(missing)} benchmark(s): {preview}{extra}")

    _warn_context_skew(columns)
    diffs = _custom_diffs([c.context for c in columns])
    term = console or Terminal()
    for c, diff in zip(columns, diffs, strict=True):
        if c.context:
            parts = [f"{c.label}:", *_ctx_summary(c.context, diff)]
            for line in fill(parts, term.width, indent="", hang="  ", sep=" "):
                term.print(sgr(line, "dim", enabled=term.color))

    # One file's benchmarks share its `path.py::` prefix: said once, in the title.
    prefix = _shared_file_prefix(shared)
    of = f" of {prefix.removesuffix('::')}" if prefix else ""
    labels = [c.label for c in columns]
    table = Table(title=f"Comparison{of} ({metric}, baseline {labels[0]})")
    table.add_column("Benchmark", flex=True)
    table.add_column(labels[0], justify="right")
    # Memory and counters: less isn't "faster".
    ratio_label = "ratio" if metric in MEMORY_METRICS or counter is not None else "speedup"
    for lbl in labels[1:]:
        table.add_column(lbl, justify="right")
        table.add_column("Δ%", justify="right")
        table.add_column(ratio_label, justify="right")

    baseline = columns[0].samples
    verdicts: list[BenchmarkVerdict] = []
    # The first file against the baseline: (direction, significant) per row.
    outcomes: list[tuple[int, bool | None]] = []

    for name in sorted(shared):
        base = baseline[name]
        row: list[Any] = [name.removeprefix(prefix), _value_cell(base, metric)]
        for idx, c in enumerate(columns[1:]):
            if name not in c.samples:
                row.extend(["-"] * 3)
                continue
            s = c.samples[name]
            result = _compare_samples(base, s, higher_is_better)
            if idx == 0 and regressions is not None:
                # compare() refuses a gate without a direction.
                verdicts.append(
                    regressions.evaluate(
                        name, result.delta * 100.0, higher_is_better=bool(higher_is_better)
                    )
                )
            significant = None if result.p_value is None else result.p_value < _SIGNIFICANCE_ALPHA
            if idx == 0:
                worse = -result.delta if higher_is_better else result.delta
                outcomes.append((0 if worse == 0 else (1 if worse > 0 else -1), significant))
            delta_cell: list[Span] = [_fmt_delta(result.delta, higher_is_better=higher_is_better)]
            if significant:
                delta_cell.append((" (signif.)", "bold"))
            row.append(_value_cell(s, metric))
            row.append(delta_cell)
            row.append("—" if result.speedup is None else f"×{result.speedup:.3f}")
        table.add_row(*row)

    term.print(table)
    summary = _summary(outcomes, metric=metric, has_direction=higher_is_better is not None)
    term.print(sgr(f"{labels[1]} against {labels[0]}: {summary}", "dim", enabled=term.color))

    if regressions is not None:
        text, code = render_panel(verdicts, default_threshold=regressions.default_threshold)
        if text:
            print(text, file=sys.stderr)
        return code
    return 0


def compare(
    files: Sequence[str | Path],
    *,
    metric: str = "real_time",
    pattern: str | None = None,
    literal: bool = False,
    regressions: RegressionConfig | None = None,
    counters: CounterConfig | None = None,
    console: Terminal | None = None,
) -> int:
    """Print a table comparing result files against the first one, the baseline.

    Each value shows its repetitions' spread, ``±N%`` of their mean. Each other
    file's column shows its value, the delta and the speedup. The rows are the
    benchmarks the baseline and the second file share. Another file that lacks
    one shows dashes in its row. A line under the table sums up the second file
    against the baseline.

    Parameters
    ----------
    files : Sequence[str or Path]
        JSON, JSONL or gzipped JSONL result files, the baseline first:
        ``compare(["base.json", "head.json"])`` compares head against base. A
        file is one sample: the runs appended to it pool as repetitions,
        with a warning when they differ in machine, interpreter, mew version
        or commit. To compare one run of an archive, extract it first.
    metric : str, default "real_time"
        ``"real_time"``, ``"cpu_time"``, or, for results with
        memory figures, ``"memory.peak_bytes"`` or
        ``"memory.allocations_per_iteration"``. The latter compares
        implementations of different speed. ``"counters.<name>"`` compares the
        counter ``<name>``, e.g. ``"counters.items_per_second"``, in the
        direction ``counters`` gives it. Without one, the delta is neither
        good nor bad, and the counter can't be gated on.
    pattern : str, optional
        Regex searched for in benchmark names.
    literal : bool, default False
        Match ``pattern`` as plain text, e.g. to keep a ``name[label]``'s brackets.
    regressions : RegressionConfig, optional
        Gate the second file against the baseline, and print a regression panel.
    counters : CounterConfig, optional
        Which direction is an improvement per counter, as ``[[tool.mew.counters]]``
        configures it. Without it, only ``items_per_second`` and
        ``bytes_per_second`` have one: higher is better.
    console : mew.Terminal, optional
        Where to print, stdout by default.

    Returns
    -------
    int
        ``0``, or ``1`` when the baseline and the second file share no
        benchmark, or ``3`` when the regression gate fails.

    Raises
    ------
    ValueError
        If an argument, such as ``pattern``, is invalid, if a file is not a
        mew result file or holds no measurements, or if ``regressions`` gates
        on a counter without a direction.
    OSError
        If a file can't be read, :class:`FileNotFoundError` if it doesn't exist.
    """
    counter = counter_name(metric)
    if metric not in METRICS and counter is None:
        raise ValueError(
            f"unknown metric {metric!r}\nhint: use {', '.join(sorted(METRICS))} or counters.<name>"
        )
    # Lower is better for times and memory; a counter goes the way `counters` says.
    higher_is_better = (
        False if counter is None else (counters or CounterConfig()).direction(counter)
    )
    if regressions is not None and higher_is_better is None:
        # A gate that skipped the counter would pass without checking anything.
        raise ValueError(
            f"{metric}: no direction to gate on\n"
            "hint: add a [[tool.mew.counters]] rule with higher-is-better"
        )
    name_filter = compile_name_filter(pattern, literal=literal) if pattern else None

    if len(files) < 2:
        raise ValueError(f"expected at least two result files, got {len(files)}")
    paths = [Path(p) for p in files]
    stems = Counter(p.stem for p in paths)

    columns = []
    for path in paths:  # the baseline first, as git diff and pyperf take it
        samples, ctx, skipped = load_column(path, metric)
        columns.append(
            _Column(
                source=str(path),
                # Files that share a stem are told apart by their full path.
                label=str(path) if stems[path.stem] > 1 else path.stem,
                samples=samples,
                context=ctx,
                skipped=skipped,
            )
        )
    return _render(
        columns,
        metric=metric,
        pattern=name_filter,
        regressions=regressions,
        console=console,
        higher_is_better=higher_is_better,
    )
