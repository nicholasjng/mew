"""Compare benchmark result files and print their differences."""

from __future__ import annotations

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
    truncate_right,
    warn,
)
from mew._paths import flatten, matches_glob
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

# How `compare` lays out its table, widest first. "auto" picks the first that fits.
LAYOUTS = ("auto", "full", "compact", "long")

# Longest a context value shows, past which it is cut with an ellipsis.
_CTX_VALUE_WIDTH = 40

# Coefficient of variation above which measurements are flagged as noisy.
_CV_UNRELIABLE = 0.25
# `host` only where a suite records it; mew doesn't, since it often names a person.


def _ctx_value(value: Any) -> str:
    """A context value as a person reads it: ``true``, ``a, b``, not Python's repr."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "-"
    if isinstance(value, list | tuple):
        return ", ".join(_ctx_value(v) for v in value)
    return truncate_right(str(value), _CTX_VALUE_WIDTH)


def _ctx_fields(ctx: dict[str, Any], shown: dict[str, str]) -> dict[str, str]:
    """A column's provenance: how many runs it pooled, its run's name and date,
    and the context values asked for.

    An unnamed run goes by its date: a short id would not tell runs apart, as a
    UUIDv7's leading digits are its time to about a minute."""
    run = ctx.get("run") or {}
    fields = {
        "runs": str(ctx.get("runs", 1)),
        "run": run.get("name") or "",
        "date": str(run.get("date") or "")[:19].replace("T", " "),
    }
    return fields | shown


def _shown_context(columns: list[_Column], patterns: Sequence[str]) -> list[dict[str, str]]:
    """Per column, the context values ``patterns`` select, by path, in pattern order.

    A value is shown whether or not it differs between the columns: a small
    change flips many, so a reader names the few that matter.
    """
    flats = [flatten(c.context.get("context") or {}) for c in columns]
    order: dict[str, int] = {}
    for path in set().union(*flats):
        if (i := matches_glob(path, list(patterns))) is not None:
            order[path] = i
    for i, pattern in enumerate(patterns):
        if i not in order.values():
            warn(f"no context field matches {pattern!r}")
    paths = sorted(order, key=lambda p: (order[p], p))
    return [{p: _ctx_value(f[p]) if p in f else "" for p in paths} for f in flats]


def _context_lines(columns: list[_Column], shown: list[dict[str, str]], term: Terminal) -> None:
    """Print the columns' provenance, the widest of three forms that fits.

    A line per file, aligned, else a line per field, else each file's fields as
    ``key=value``, wrapped. The run count shows only if a file pooled runs, and
    the name only if a run has one.
    """
    rows = {c.label: _ctx_fields(c.context, d) for c, d in zip(columns, shown, strict=True)}
    keys = list(dict.fromkeys(k for fields in rows.values() for k in fields))
    if all(f.get("runs", "1") == "1" for f in rows.values()):
        keys.remove("runs")
    if not any(f.get("run") for f in rows.values()):
        keys.remove("run")

    def cell(text: str) -> list[Span]:
        return [(text or "-", "dim")]

    by_file = Table()
    by_file.add_column("")
    for key in keys:
        by_file.add_column(key)
    for label, fields in rows.items():
        by_file.add_row(label, *(cell(fields.get(k, "")) for k in keys))
    by_field = Table()
    by_field.add_column("")
    for label in rows:
        by_field.add_column(label)
    for key in keys:
        by_field.add_row(key, *(cell(fields.get(key, "")) for fields in rows.values()))
    for table in (by_file, by_field):
        if table.min_width() <= term.width:
            term.print(table)
            return
    for label, fields in rows.items():
        parts = [f"{label}:", *(f"{k}={fields[k]}" for k in keys if fields.get(k))]
        for line in fill(parts, term.width, indent="", hang="  ", sep=" "):
            term.print(sgr(line, "dim", enabled=term.color))


def _warn_context_skew(columns: list[_Column]) -> None:
    """Warn when machine-level context differs across columns.

    The deltas then compare environments, not just code.
    """
    for fld in SKEW_FIELDS:
        values = {c.label: (c.context.get("context") or {}).get(fld) for c in columns if c.context}
        if len({v for v in values.values() if v is not None}) > 1:
            detail = ", ".join(f"{label}: {v}" for label, v in values.items())
            warn(f"result files differ in {fld}: {detail}")


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


def _fmt_delta(
    delta: float, *, higher_is_better: bool | None = False, precision: int = 2
) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.{precision}f}%" if math.isfinite(pct) else "+∞%" if pct > 0 else "-∞%"
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


@dataclass(frozen=True, slots=True)
class _Cmp:
    """One file's sample of a benchmark, compared against the baseline's."""

    sample: Sample
    result: _Comparison
    #: None without a significance test, at fewer than 2 repetitions a side.
    significant: bool | None


@dataclass(frozen=True, slots=True)
class _Row:
    """A benchmark's row: the baseline's sample and each other file's, None where missing."""

    name: str
    base: Sample
    others: list[_Cmp | None]


def _ratio_text(result: _Comparison) -> str:
    return "—" if result.speedup is None else f"×{result.speedup:.3f}"


def _delta_cell(c: _Cmp, higher_is_better: bool | None) -> list[Span]:
    cell: list[Span] = [_fmt_delta(c.result.delta, higher_is_better=higher_is_better)]
    if c.significant:
        cell.append((" (signif.)", "bold"))
    return cell


def _full_table(
    title: str,
    labels: list[str],
    rows: list[_Row],
    *,
    metric: str,
    higher_is_better: bool | None,
    ratio_label: str,
) -> Table:
    """Every file's value, delta and ratio side by side: about 37 columns a file."""
    table = Table(title=title)
    table.add_column("Benchmark", flex=True)
    table.add_column(labels[0], justify="right")
    for lbl in labels[1:]:
        table.add_column(lbl, justify="right")
        table.add_column("Δ%", justify="right")
        table.add_column(ratio_label, justify="right")
    for r in rows:
        cells: list[Any] = [r.name, _value_cell(r.base, metric)]
        for c in r.others:
            if c is None:
                cells.extend(["-"] * 3)
                continue
            cells.append(_value_cell(c.sample, metric))
            cells.append(_delta_cell(c, higher_is_better))
            cells.append(_ratio_text(c.result))
        table.add_row(*cells)
    return table


def _compact_cell(c: _Cmp, *, higher_is_better: bool | None, speedup: bool) -> list[Span]:
    """A file's delta, or its ratio, in one short cell.

    A significant change is colored and marked ``▲`` or ``▼``, the way the shown
    number moved. One that isn't is dim. Without a test the cell is colored as
    in the full table. A spread too wide to trust adds a red ``(!)``.
    """
    pct, style = _fmt_delta(c.result.delta, higher_is_better=higher_is_better, precision=1)
    if speedup:
        text, up = _ratio_text(c.result), (c.result.speedup or 1.0) > 1.0
    else:
        text, up = pct, c.result.delta > 0
    if c.significant is False:
        style = "dim"
    cell: list[Span] = [(text, style or None)]
    if c.significant:
        cell.append(("▲" if up else "▼", style or None))
    if c.sample.cv is not None and c.sample.cv >= _CV_UNRELIABLE:
        cell.append((" (!)", "red"))
    return cell


def _compact_table(
    title: str,
    labels: list[str],
    rows: list[_Row],
    *,
    metric: str,
    higher_is_better: bool | None,
    speedup: bool,
) -> Table:
    """The baseline's values, then one cell a file: about 9 columns a file.

    The title names the metric and each cell its unit, so there is no ratio label.
    """
    table = Table(title=title)
    table.add_column("Benchmark", flex=True)
    table.add_column(labels[0], justify="right")
    for lbl in labels[1:]:
        table.add_column(lbl, justify="right")
    for r in rows:
        cells: list[Any] = [r.name, _value_cell(r.base, metric)]
        cells += [
            "-"
            if c is None
            else _compact_cell(c, higher_is_better=higher_is_better, speedup=speedup)
            for c in r.others
        ]
        table.add_row(*cells)
    return table


def _long_table(
    title: str,
    labels: list[str],
    rows: list[_Row],
    *,
    metric: str,
    higher_is_better: bool | None,
    ratio_label: str,
) -> Table:
    """A line per benchmark and file, the full table's figures: as wide for any count."""
    table = Table(title=title)
    table.add_column("Benchmark", flex=True)
    table.add_column("File")
    table.add_column("Value", justify="right")
    table.add_column("Δ%", justify="right")
    table.add_column(ratio_label, justify="right")
    for r in rows:
        table.add_row(r.name, labels[0], _value_cell(r.base, metric), "", "")
        for lbl, c in zip(labels[1:], r.others, strict=True):
            if c is None:
                table.add_row("", lbl, "-", "-", "-")
            else:
                table.add_row(
                    "",
                    lbl,
                    _value_cell(c.sample, metric),
                    _delta_cell(c, higher_is_better),
                    _ratio_text(c.result),
                )
    return table


def _render(
    columns: list[_Column],
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    regressions: RegressionConfig | None,
    console: Terminal | None,
    higher_is_better: bool | None,
    layout: str = "auto",
    speedup: bool = False,
    show_context: Sequence[str] = (),
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
    shown = _shown_context(columns, show_context)
    term = console or Terminal()
    if any(c.context for c in columns):
        _context_lines(columns, shown, term)
        term.print("")

    # One file's benchmarks share its `path.py::` prefix: said once, in the title.
    prefix = _shared_file_prefix(shared)
    of = f" of {prefix.removesuffix('::')}" if prefix else ""
    labels = [c.label for c in columns]
    title = f"Comparison{of} ({metric}, baseline {labels[0]})"

    baseline = columns[0].samples
    verdicts: list[BenchmarkVerdict] = []
    # The first file against the baseline: (direction, significant) per row.
    outcomes: list[tuple[int, bool | None]] = []
    rows: list[_Row] = []

    for name in sorted(shared):
        base = baseline[name]
        others: list[_Cmp | None] = []
        for idx, c in enumerate(columns[1:]):
            if name not in c.samples:
                others.append(None)
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
            others.append(_Cmp(s, result, significant))
        rows.append(_Row(name.removeprefix(prefix), base, others))

    # Memory and counters: less isn't "faster".
    ratio_label = "ratio" if metric in MEMORY_METRICS or counter is not None else "speedup"
    hib = higher_is_better
    tables = {
        "full": _full_table(
            title, labels, rows, metric=metric, higher_is_better=hib, ratio_label=ratio_label
        ),
        "compact": _compact_table(
            title, labels, rows, metric=metric, higher_is_better=hib, speedup=speedup
        ),
        "long": _long_table(
            title, labels, rows, metric=metric, higher_is_better=hib, ratio_label=ratio_label
        ),
    }
    if layout == "auto":
        # The widest layout that fits: long grows down, so it is the last resort.
        table = next((t for t in tables.values() if t.min_width() <= term.width), tables["long"])
    else:
        table = tables[layout]
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
    layout: str = "auto",
    speedup: bool = False,
    show_context: Sequence[str] = (),
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
    layout : {"auto", "full", "compact", "long"}, default "auto"
        ``"full"`` shows each file's value, delta and ratio. ``"compact"`` shows
        one cell per file, its delta marked ``▲`` or ``▼`` where significant
        and dim where not. ``"long"`` gives each file its own line under each
        benchmark. ``"auto"`` picks the first of these that fits the width.
    speedup : bool, default False
        In the compact layout, show each file's ratio instead of its delta.
    show_context : Sequence[str], default ()
        Context fields to show above the table, by path or glob, such as
        ``"vcs.commit"`` or ``"jax.*"``. A path selects every field below it.
        Without one, only each file's run name, date and run count show.
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
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}\nhint: use {', '.join(LAYOUTS)}")
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
        layout=layout,
        speedup=speedup,
        show_context=show_context,
    )
