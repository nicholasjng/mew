"""Decode result files, normalize metadata, and select a session's comparable samples."""

from __future__ import annotations

import dataclasses
import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from mew._console import overflow
from mew._statistics import Statistic, reduce_statistic
from mew._typing import BenchmarkResult
from mew.reporter import _ROW_STAMP_FIELDS, canonical_row_name

_NS_PER_UNIT = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}
_TIME_METRICS = frozenset({"real_time", "cpu_time"})


def _to_ns(value: float, unit: str | None) -> float:
    """Normalize a ``(value, unit)`` pair to nanoseconds.

    Google Benchmark reports every value in one declared unit (``ns`` unless
    the benchmark calls ``SetTimeUnit``); two files being compared can declare
    different units (e.g. one produced with ``--benchmark_time_unit=us``), so
    delta/speedup math must go through this, not raw ``sample.value``.
    """
    return value * _NS_PER_UNIT.get(unit or "ns", 1.0)


# `memory.total_bytes` and `memory.total_allocations` may stay in stored files
# but are not compare metrics: total allocated bytes describes cumulative work,
# and total allocations is not comparable across differing iteration counts
# (`allocations_per_iteration` is the comparable form).
_MEMORY_METRICS = frozenset(
    {
        "memory.peak_bytes",
        "memory.allocations_per_iteration",
    }
)
_METRICS = frozenset({"real_time", "cpu_time", "iterations"}) | _MEMORY_METRICS


@dataclass(frozen=True, slots=True)
class Sample:
    """A reduced benchmark measurement.

    Attributes
    ----------
    name : str
        Canonical ``file.py::func[label]`` name, re-keyed per the match key.
    value : float
        Center across the benchmark's per-repetition rows (median by default).
    stddev : float or None
        Sample stddev across repetitions; ``None`` for a single repetition.
    time_unit : str or None
        Unit ``value`` is expressed in; ``None`` for unitless metrics.
    values : tuple[float, ...]
        Raw per-repetition values (same unit as ``value``), feeding the
        Mann-Whitney significance marker. A single repetition has one value.
    """

    name: str
    value: float
    stddev: float | None
    time_unit: str | None
    values: tuple[float, ...] = ()

    @property
    def cv(self) -> float | None:
        """Coefficient of variation, or None without repetition data."""
        if self.stddev is None or not self.value:
            return None
        return self.stddev / abs(self.value)


def _is_aggregate_row(row: dict[str, Any]) -> bool:
    return bool(row.get("aggregate_name"))


def _is_measurement_row(row: dict[str, Any]) -> bool:
    """Return whether a row is a successful, non-aggregate measurement."""
    return (
        isinstance(row.get("name"), str) and not _is_aggregate_row(row) and not row.get("skipped")
    )


def _check_row(obj: Any, where: str) -> None:
    """Reject anything but a result row: an object with ``name`` and ``benchmark``."""
    if not isinstance(obj, dict) or "name" not in obj or "benchmark" not in obj:
        raise ValueError(
            f"{where}: expected a mew result row (a JSON object with 'name' and 'benchmark')"
        )


def _rows_from_json(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON: {e}") from e
    rows = doc.get("benchmarks") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: missing 'benchmarks' array")
    for i, row in enumerate(rows):
        _check_row(row, f"{path}: benchmarks[{i}]")
    return rows


def _rows_from_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL sink (plain or gzip): one self-contained row per line."""
    rows: list[dict[str, Any]] = []
    if path.name.endswith(".gz"):
        import gzip

        def _open(p: Path) -> TextIO:
            return gzip.open(p, "rt")
    else:
        _open = Path.open
    # Stream line-by-line: a growing --append archive can be large, and
    # read_text() would hold the whole file in memory on top of the parsed rows.
    with _open(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
            _check_row(obj, f"{path}:{lineno}")
            rows.append(obj)
    return rows


def _session_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(date, host, id)`` for a result row."""
    sess = _session_block(row)
    return (str(sess.get("date") or ""), str(sess.get("host") or ""), str(sess.get("id") or ""))


def _session_block(row: dict[str, Any]) -> dict[str, Any]:
    """The normalized row's session block."""
    block = row.get("session") or {}
    return block if isinstance(block, dict) else {}


def _metric_value(row: dict[str, Any], metric: str) -> Any:
    """Look up ``metric`` in a row; dotted metrics traverse one nested level.

    Nested blocks (e.g. ``memory``) are already decoded to dicts at the read
    boundary (see :func:`_read_rows`).
    """
    head, sep, tail = metric.partition(".")
    value = row.get(head)
    if not sep:
        return value
    return value.get(tail) if isinstance(value, dict) else None


def _metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    """Per-repetition values in the first row's unit, dropping absent metrics."""
    # Reduce in the first row's unit, preserving the public Sample unit while
    # making appended sessions with different declared units comparable.
    unit_scale = _NS_PER_UNIT.get(rows[0].get("time_unit") or "ns", 1.0) if rows else 1.0
    return [
        _to_ns(float(v), r.get("time_unit")) / unit_scale if metric in _TIME_METRICS else float(v)
        for r in rows
        if (v := _metric_value(r, metric)) is not None
    ]


def _aggregate_values(
    values: list[float], statistic: Statistic | None = None
) -> tuple[float, float | None]:
    """Return the selected center and sample standard deviation of measurements."""
    center = (
        reduce_statistic(statistic, values) if statistic is not None else statistics.median(values)
    )
    stddev = statistics.stdev(values) if len(values) > 1 else None
    return center, stddev


def _normalize_name(name: str, key: str) -> str:
    """``key="func"`` strips the ``file.py::`` prefix from a registered name."""
    if key == "func":
        return name.rsplit("::", 1)[-1]
    return name


def _normalize_samples(samples: dict[str, Sample], key: str, source: str) -> dict[str, Sample]:
    """Re-key samples for the requested match key, erroring on collisions."""
    if key == "name":
        return samples
    renamed: dict[str, Sample] = {}
    origin: dict[str, str] = {}
    for full, sample in samples.items():
        short = _normalize_name(full, key)
        if short in renamed:
            raise SystemExit(
                f"{source}: --key {key} maps both {origin[short]!r} and {full!r} "
                f"to {short!r}; disambiguate or use the default --key name"
            )
        renamed[short] = dataclasses.replace(sample, name=short)
        origin[short] = full
    return renamed


def _read_rows(path: Path) -> list[dict[str, Any]]:
    """Dispatch on suffix to read a result file's rows.

    A missing or unparseable input is a CLI-level error, so read/parse failures
    surface as ``SystemExit`` with a one-line message, not a traceback.
    """
    name = path.name.lower()
    if name.endswith(".json"):
        reader = _rows_from_json
    elif name.endswith((".jsonl", ".jsonl.gz")):
        reader = _rows_from_jsonl
    else:
        raise SystemExit(f"unsupported result file: {path} (use .json, .jsonl, or .jsonl.gz)")
    try:
        return reader(path)
    except FileNotFoundError as e:
        raise SystemExit(f"result file not found: {path}") from e
    except OSError as e:
        raise SystemExit(f"cannot read result file {path}: {e.strerror or e}") from e
    except ValueError as e:
        # _rows_from_* messages already carry path (and line) context.
        raise SystemExit(str(e)) from e


def _samples_from_groups(
    groups: dict[str, list[dict[str, Any]]],
    metric: str,
    statistic: Statistic | None = None,
) -> dict[str, Sample]:
    """Aggregate per-name row groups into center/stddev :class:`Sample`s."""
    samples: dict[str, Sample] = {}
    for name, group in groups.items():
        values = _metric_values(group, metric)
        if not values:
            continue
        try:
            center, stddev = _aggregate_values(values, statistic)
        except statistics.StatisticsError as e:
            # A reducer can reject its input (e.g. gmean with zero timings).
            # Report that failure instead of dropping the benchmark.
            raise SystemExit(f"--statistic failed on {name!r}: {e}") from e
        samples[name] = Sample(
            name=name,
            value=center,
            stddev=stddev,
            time_unit=group[0].get("time_unit"),
            values=tuple(values),
        )
    return samples


def _group_by_session(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Group successful measurement rows by session, oldest key first.

    ISO-8601 dates lead the key, so it sorts chronologically.
    """
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if _is_measurement_row(r):
            buckets.setdefault(_session_key(r), []).append(r)
    return dict(sorted(buckets.items()))


def _group_by_name(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket rows by canonical ``name[label]``, the unit both load paths aggregate over."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(canonical_row_name(r), []).append(r)
    return groups


def _split_selector(raw: str) -> tuple[Path, str | None]:
    """Split ``path@selector``, preserving existing paths that contain ``@``."""
    if Path(raw).exists():
        return Path(raw), None
    base, sep, selector = raw.rpartition("@")
    if not sep or not base:
        return Path(raw), None
    return Path(base), selector


def _select_rows(
    path: Path,
    by_session: dict[tuple[str, str, str], list[dict[str, Any]]],
    selector: str | None,
) -> list[dict[str, Any]]:
    """Pick the rows a ``path@selector`` names.

    ``None`` and ``latest`` select the newest session. Any other selector is a
    session tag; every session carrying it is selected, so repeated runs under
    one tag pool as repetitions. ``mew sessions`` lists what a file holds.
    """
    if not by_session:
        raise SystemExit(f"{path}: no measurements in file")
    if selector is None or selector == "latest":
        return by_session[max(by_session)]
    if not selector:
        raise SystemExit(f"{path}: empty session selector after '@'")
    tagged = [
        rows for rows in by_session.values() if _session_block(rows[0]).get("tag") == selector
    ]
    if not tagged:
        tags = sorted(
            {t for rows in by_session.values() if (t := _session_block(rows[0]).get("tag"))}
        )
        shown = ", ".join(tags[:5]) + overflow(len(tags), 5)
        hint = f" (tags in file: {shown})" if tags else " (no tagged sessions in file)"
        raise SystemExit(f"{path}: no session tagged {selector!r}{hint}; see `mew sessions {path}`")
    return [r for rows in tagged for r in rows]


Reader = Callable[[Path], list[dict[str, Any]]]


def _load(
    path: Path,
    metric: str,
    key: str = "name",
    selector: str | None = None,
    statistic: Statistic | None = None,
    reader: Reader = _read_rows,
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Load one comparison column from a result file: read, select a session, re-key.

    ``reader`` lets a caller comparing several selectors of one file parse it once.
    """
    by_session = _group_by_session(reader(path))
    selected = _select_rows(path, by_session, selector)
    # The newest selected row speaks for the column's provenance.
    rep_row = max(selected, key=_session_key)
    samples = _samples_from_groups(_group_by_name(selected), metric, statistic)
    ctx = {key: rep_row[key] for key in _ROW_STAMP_FIELDS if key in rep_row}
    return _normalize_samples(samples, key, str(path)), ctx


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """One session of a result file, as ``mew sessions`` lists it."""

    id: str | None
    date: str | None
    host: str | None
    tag: str | None
    benchmarks: int
    rows: int


def session_summaries(path: str | Path) -> list[SessionSummary]:
    """Summarize every session in a result file, newest first.

    ``rows`` counts every stored row of the session; ``benchmarks`` counts
    distinct measured benchmarks (aggregate and skipped rows excluded).
    """
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in _read_rows(Path(path)):
        buckets.setdefault(_session_key(r), []).append(r)
    out: list[SessionSummary] = []
    for (date, host, sid), session_rows in sorted(buckets.items(), reverse=True):
        measured = {canonical_row_name(r) for r in session_rows if _is_measurement_row(r)}
        out.append(
            SessionSummary(
                id=sid or None,
                date=date or None,
                host=host or None,
                tag=_session_block(session_rows[0]).get("tag"),
                benchmarks=len(measured),
                rows=len(session_rows),
            )
        )
    return out


def read_results(path: str | Path) -> list[BenchmarkResult]:
    """Read result rows in file order.

    Accepts JSON, JSONL, and gzip-compressed results; every row carries its own
    ``session`` and ``context``.

    Parameters
    ----------
    path : str or Path
        Result file to read.

    Returns
    -------
    list[BenchmarkResult]
        Stored rows, including aggregate and skipped rows.
    """
    return cast("list[BenchmarkResult]", _read_rows(Path(path)))
