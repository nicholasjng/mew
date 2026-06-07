"""Compare benchmark result files: deltas, speedups, optional stddev."""

from __future__ import annotations

import json
import statistics
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from mew.regressions import BenchmarkVerdict, RegressionConfig, report

_METRICS = frozenset({"real_time", "cpu_time", "iterations"})
_HIGHER_IS_BETTER = frozenset({"iterations"})


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    value: float
    stddev: float | None
    time_unit: str | None
    session_date: str | None


def _is_aggregate_row(row: dict[str, Any]) -> bool:
    return bool(row.get("aggregate_name"))


def _rows_from_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    doc = json.loads(path.read_text())
    benchmarks = doc.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError(f"{path}: missing 'benchmarks' array")
    ctx = doc.get("context") or {}
    return benchmarks, ctx


def _rows_from_parquet(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if find_spec("pyarrow") is None:
        raise SystemExit(
            "pyarrow is required to read Parquet result files. "
            "Install it with: uv add --optional parquet pyarrow"
        )
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist(), {}


def _session_key(row: dict[str, Any], file_ctx: dict[str, Any]) -> tuple[str, str]:
    """Identify which session a row belongs to.

    Parquet rows carry session columns per-row.
    JSON rows inherit the file's single top-level context block via ``file_ctx``.
    """
    date = row.get("date") or file_ctx.get("date") or ""
    host = row.get("host_name") or file_ctx.get("host_name") or ""
    return (str(date), str(host))


def _aggregate_group(rows: list[dict[str, Any]], metric: str) -> tuple[float, float | None]:
    """Median and (sample) stddev across a group of per-repetition rows."""
    values = [float(r[metric]) for r in rows if r.get(metric) is not None]
    if not values:
        raise ValueError(f"no {metric!r} values in group")
    median = statistics.median(values)
    stddev = statistics.stdev(values) if len(values) > 1 else None
    return median, stddev


def _load(path: Path, metric: str) -> dict[str, Sample]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        rows, file_ctx = _rows_from_json(path)
    elif suffix in (".parquet", ".pq"):
        rows, file_ctx = _rows_from_parquet(path)
    else:
        raise SystemExit(f"unsupported result file: {path} (use .json or .parquet)")

    # Group per-repetition rows by (name, session). GB-emitted aggregate rows
    # are dropped — we recompute statistics ourselves so the result is
    # consistent whether the file was produced with --repetitions=1 or N.
    by_group: dict[tuple[str, tuple[str, str]], list[dict[str, Any]]] = {}
    for r in rows:
        name = r.get("name")
        if not isinstance(name, str) or _is_aggregate_row(r):
            continue
        key = (name, _session_key(r, file_ctx))
        by_group.setdefault(key, []).append(r)

    # If a name has multiple sessions in the same file, keep the latest by
    # ISO-8601 date (lexicographic order matches chronological for ISO).
    sessions_per_name: dict[str, list[tuple[str, str]]] = {}
    for name, session in by_group:
        sessions_per_name.setdefault(name, []).append(session)

    samples: dict[str, Sample] = {}
    for name, sessions in sessions_per_name.items():
        if len(sessions) > 1:
            sessions.sort(key=lambda s: s[0])  # ascending date
            chosen = sessions[-1]
            print(
                f"warning: {path}: {name!r} has {len(sessions)} sessions, "
                f"keeping latest (date={chosen[0]!r}, host={chosen[1]!r})",
                file=sys.stderr,
            )
        else:
            chosen = sessions[0]
        group = by_group[(name, chosen)]
        try:
            median, stddev = _aggregate_group(group, metric)
        except ValueError:
            continue
        samples[name] = Sample(
            name=name,
            value=median,
            stddev=stddev,
            time_unit=group[0].get("time_unit"),
            session_date=chosen[0] or None,
        )
    return samples


def _label(path: Path, others: list[Path]) -> str:
    stem = path.stem
    if sum(1 for p in others if p.stem == stem) > 1:
        return str(path)
    return stem


def _fmt_value(sample: Sample, metric: str) -> str:
    if metric == "iterations":
        return f"{int(sample.value):,}"
    unit = sample.time_unit or ""
    return f"{sample.value:.2f} {unit}".rstrip()


def _fmt_delta(delta: float) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.2f}%"
    style = "green" if delta < 0 else "red" if delta > 0 else ""
    return text, style


def _fmt_speedup(speedup: float) -> str:
    return f"×{speedup:.3f}"


def compare(
    files: list[Path],
    *,
    metric: str = "real_time",
    pattern: str | None = None,
    show_stddev: bool = False,
    regressions: RegressionConfig | None = None,
    console: Console | None = None,
) -> int:
    """Compare benchmark result files and render a comparison table.

    The first file is the baseline; later files are reported as percent deltas and speedups.

    Parameters
    ----------
    files : list[Path]
        Result files (JSON or Parquet); the first is treated as the baseline.
    metric : str, default "real_time"
        Metric to compare.
        One of ``"real_time"``, ``"cpu_time"``, ``"iterations"``.
    pattern : str, optional
        Substring filter applied to benchmark names.
    show_stddev : bool, default False
        Add per-file stddev columns when stddev data is present.
    regressions : RegressionConfig, optional
        If given, gate the second file against the baseline and append a regression panel.
    console : rich.console.Console, optional
        Output console; defaults to a fresh :class:`~rich.console.Console`.

    Returns
    -------
    int
        Process exit code: ``0`` on success, ``1`` for no overlap, ``2`` if the regression gate fails.
    """
    if metric not in _METRICS:
        raise SystemExit(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    if len(files) < 2:
        raise SystemExit("mew compare needs at least two result files")

    loaded = [(p, _load(p, metric)) for p in files]
    all_names: set[str] = set().union(*(s.keys() for _, s in loaded))
    if pattern:
        all_names = {n for n in all_names if pattern in n}

    shared = set.intersection(*(set(s.keys()) for _, s in loaded)) & all_names
    if not shared:
        print("no overlapping benchmarks across input files", file=sys.stderr)
        return 1

    for path, samples in loaded:
        missing = all_names - set(samples.keys())
        if missing:
            preview = ", ".join(sorted(missing)[:5])
            extra = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
            print(
                f"warning: {path} missing {len(missing)} benchmark(s): {preview}{extra}",
                file=sys.stderr,
            )

    baseline_path, baseline = loaded[0]
    others = loaded[1:]
    labels = [_label(p, [q for q, _ in loaded]) for p, _ in loaded]

    console = console or Console()
    table = Table(title=f"Comparison ({metric})", show_lines=False)
    table.add_column("Benchmark", overflow="fold")
    table.add_column(f"{labels[0]} (baseline)", justify="right")
    if show_stddev:
        table.add_column("± stddev", justify="right")
    for lbl in labels[1:]:
        table.add_column(f"{lbl} Δ%", justify="right")
        table.add_column("speedup", justify="right")
        if show_stddev:
            table.add_column("± stddev", justify="right")

    higher_is_better = metric in _HIGHER_IS_BETTER
    verdicts: list[BenchmarkVerdict] = []

    for name in sorted(shared):
        base = baseline[name]
        row: list[Any] = [name, _fmt_value(base, metric)]
        if show_stddev:
            row.append(f"{base.stddev:.2f}" if base.stddev is not None else "-")
        for idx, (_, samples) in enumerate(others):
            s = samples[name]
            delta = (s.value - base.value) / base.value if base.value else 0.0
            speedup = base.value / s.value if s.value else float("inf")
            delta_text, delta_style = _fmt_delta(delta)
            row.append(f"[{delta_style}]{delta_text}[/]" if delta_style else delta_text)
            row.append(_fmt_speedup(speedup))
            if show_stddev:
                row.append(f"{s.stddev:.2f}" if s.stddev is not None else "-")
            # Gate only against the first non-baseline column — the rightmost
            # columns are informational in a multi-file comparison.
            if regressions is not None and idx == 0:
                verdicts.append(
                    regressions.evaluate(name, delta * 100.0, higher_is_better=higher_is_better)
                )
        table.add_row(*row)

    console.print(table)

    if regressions is not None:
        return report(verdicts, default_threshold_pct=regressions.default_threshold_pct)
    return 0
