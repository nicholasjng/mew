"""Read result files, group their rows by session, and reduce them to samples."""

from __future__ import annotations

import dataclasses
import json
import statistics
import zlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from mew._format import overflow, to_ns
from mew._statistics import Statistic
from mew._typing import BenchmarkResult

# Stamped onto every row so each one stands alone, in JSON and JSONL alike.
ROW_STAMP_FIELDS = ("session", "context")


def result_format(name: str) -> Literal["json", "jsonl"] | None:
    """``"json"`` or ``"jsonl"`` by a result file's extension, in any case; else ``None``."""
    name = name.lower()
    if name.endswith(".json"):
        return "json"
    if name.endswith((".jsonl", ".jsonl.gz")):
        return "jsonl"
    return None


def open_text(path: Path, mode: str = "r") -> TextIO:
    """Open ``path`` as text, through gzip when its name ends in ``.gz`` in any case."""
    if path.name.lower().endswith(".gz"):
        import gzip

        return cast(TextIO, gzip.open(path, mode + "t"))
    return cast(TextIO, path.open(mode))


TIME_METRICS = frozenset({"real_time", "cpu_time"})


# Totals scale with the iteration count, so only peak and per-iteration memory
# are comparable.
MEMORY_METRICS = frozenset(
    {
        "memory.peak_bytes",
        "memory.allocations_per_iteration",
    }
)
METRICS = frozenset({"real_time", "cpu_time", "iterations"}) | MEMORY_METRICS


@dataclass(frozen=True, slots=True)
class Sample:
    """One benchmark's measurement in one file, reduced over its repetitions.

    Attributes
    ----------
    name : str
        ``file.py::func[label]``, without ``file.py::`` when matching by function.
    value : float
        The statistic over the repetitions, the median by default.
    stddev : float or None
        Sample stddev across repetitions; ``None`` for a single repetition.
    time_unit : str or None
        The unit of ``value`` for ``real_time`` and ``cpu_time``, e.g. ``"ns"``;
        ``None`` for the other metrics, which are counts or bytes.
    values : tuple[float, ...]
        The per-repetition values, in ``value``'s unit, for the significance test.
    """

    name: str
    value: float
    stddev: float | None
    time_unit: str | None
    values: tuple[float, ...] = ()

    @property
    def cv(self) -> float | None:
        """The coefficient of variation, ``stddev`` over the mean of ``values``.

        Uses the mean whatever statistic ``value`` holds, so the choice of
        statistic does not move the noise marker. ``None`` without repetitions
        or for a zero mean.
        """
        if self.stddev is None or not self.values:
            return None
        mean = statistics.fmean(self.values)
        return self.stddev / abs(mean) if mean else None


def _is_measurement_row(row: dict[str, Any]) -> bool:
    """Return whether a row is a successful measurement.

    Google Benchmark's aggregate rows (``_mean``, ``_median``, ...), which some
    result files hold, are not: mew reduces repetitions itself.
    """
    return (
        isinstance(row.get("name"), str)
        and not row.get("aggregate_name")
        and not row.get("skipped")
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
        raise ValueError(f"{path}: missing 'benchmarks' array")  # ruff: ignore[TRY004]
    for i, row in enumerate(rows):
        _check_row(row, f"{path}: benchmarks[{i}]")
    return rows


def _rows_from_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL sink (plain or gzip): one self-contained row per line."""
    rows: list[dict[str, Any]] = []
    # Stream: a growing --append archive can be large.
    with open_text(path) as fh:
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
    """Return ``(date, id, host)`` for a result row.

    Sorts oldest first: the ISO-8601 date leads, and the time-ordered UUIDv7 id
    breaks ties between equal dates.
    """
    sess = _session_block(row)
    return (str(sess.get("date") or ""), str(sess.get("id") or ""), str(sess.get("host") or ""))


def _session_block(row: dict[str, Any]) -> dict[str, Any]:
    """The normalized row's session block."""
    block = row.get("session") or {}
    return block if isinstance(block, dict) else {}


def _metric_value(row: dict[str, Any], metric: str) -> Any:
    """Look up ``metric`` in a row; dotted metrics traverse one nested level."""
    head, sep, tail = metric.partition(".")
    value = row.get(head)
    if not sep:
        return value
    return value.get(tail) if isinstance(value, dict) else None


def _metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    """Per-repetition values in the first row's unit, dropping absent metrics."""
    # Appended sessions may declare different units.
    unit_scale = to_ns(1.0, rows[0].get("time_unit"))
    return [
        to_ns(float(v), r.get("time_unit")) / unit_scale if metric in TIME_METRICS else float(v)
        for r in rows
        if (v := _metric_value(r, metric)) is not None
    ]


def _aggregate_values(
    values: list[float], statistic: Statistic | None = None
) -> tuple[float, float | None]:
    """Return the selected center and sample standard deviation of measurements."""
    # A user reducer may return e.g. a numpy scalar.
    center = float((statistic or statistics.median)(values))
    stddev = statistics.stdev(values) if len(values) > 1 else None
    return center, stddev


def _normalize_samples(samples: dict[str, Sample], key: str, source: str) -> dict[str, Sample]:
    """Re-key samples for the requested match key, erroring on collisions.

    ``key="func"`` strips the ``file.py::`` prefix from each registered name.
    """
    if key == "name":
        return samples
    renamed: dict[str, Sample] = {}
    origin: dict[str, str] = {}
    for full, sample in samples.items():
        short = full.rsplit("::", 1)[-1]
        if short in renamed:
            raise ValueError(
                f"{source}: --key {key} maps both {origin[short]!r} and {full!r} "
                f"to {short!r}; disambiguate or use the default --key name"
            )
        renamed[short] = dataclasses.replace(sample, name=short)
        origin[short] = full
    return renamed


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a result file's rows.

    Errors carry a one-line message naming the file: :class:`ValueError` for an
    unsupported extension or malformed contents, :class:`OSError` (of the
    original subclass) if the file can't be read.
    """
    fmt = result_format(path.name)
    if fmt is None:
        raise ValueError(f"unsupported result file: {path} (use .json, .jsonl, or .jsonl.gz)")
    reader = _rows_from_json if fmt == "json" else _rows_from_jsonl
    try:
        return reader(path)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"result file not found: {path}") from e
    except OSError as e:
        raise type(e)(f"cannot read result file {path}: {e.strerror or e}") from e
    except (EOFError, zlib.error) as e:  # a truncated or corrupt archive
        raise ValueError(f"cannot read result file {path}: {e}") from e


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
            # E.g. gmean over a zero before Python 3.13; fail rather than drop the benchmark.
            raise ValueError(f"statistic failed on {name!r}: {e}") from e
        samples[name] = Sample(
            name=name,
            value=center,
            stddev=stddev,
            time_unit=group[0].get("time_unit") if metric in TIME_METRICS else None,
            values=tuple(values),
        )
    return samples


def _group_by_session(
    rows: list[dict[str, Any]], *, measured_only: bool = True
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Group rows by session, oldest key first; only successful measurements by default.

    ISO-8601 dates lead the key, so it sorts chronologically.
    """
    buckets: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if not measured_only or _is_measurement_row(r):
            buckets[_session_key(r)].append(r)
    return dict(sorted(buckets.items()))


def split_selector(raw: str) -> tuple[Path, str | None]:
    """Split ``path@selector`` at the last ``@``, if a result-file extension precedes it.

    Otherwise the whole argument is the path, so ``a@b.json`` names a file.
    Decided by name alone, so an argument means the same whichever files exist.
    """
    base, sep, selector = raw.rpartition("@")
    if sep and result_format(base) is not None:
        return Path(base), selector
    return Path(raw), None


def _select_rows(
    path: Path,
    by_session: dict[tuple[str, str, str], list[dict[str, Any]]],
    selector: str | None,
) -> list[dict[str, Any]]:
    """Pick the rows a ``path@selector`` names.

    ``None`` and ``latest`` select the newest session; :func:`mew.run` rejects
    ``latest`` as a session tag. Any other selector is a session tag; every
    session carrying it is selected, so repeated runs under one tag pool as
    repetitions. ``mew sessions`` lists what a file holds.
    """
    if not by_session:
        raise ValueError(f"{path}: no measurements in file")
    if selector is None or selector == "latest":
        return by_session[max(by_session)]
    if not selector:
        raise ValueError(f"{path}: empty session selector after '@'")
    tagged = [
        rows for rows in by_session.values() if _session_block(rows[0]).get("tag") == selector
    ]
    if not tagged:
        tags = sorted(
            {t for rows in by_session.values() if (t := _session_block(rows[0]).get("tag"))}
        )
        shown = ", ".join(tags[:5]) + overflow(len(tags), 5)
        hint = f" (tags in file: {shown})" if tags else " (no tagged sessions in file)"
        raise ValueError(f"{path}: no session tagged {selector!r}{hint}; see `mew sessions {path}`")
    return [r for rows in tagged for r in rows]


def load_column(
    path: Path,
    metric: str,
    key: str = "name",
    selector: str | None = None,
    statistic: Statistic | None = None,
    reader: Callable[[Path], list[dict[str, Any]]] = read_rows,
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Load one comparison column from a result file: read, select a session, re-key.

    ``reader`` lets a caller comparing several selectors of one file parse it once.
    """
    by_session = _group_by_session(reader(path))
    selected = _select_rows(path, by_session, selector)
    # The newest selected row speaks for the column's provenance.
    rep_row = max(selected, key=_session_key)
    # Repetitions of one benchmark share its `name[label]`.
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in selected:
        groups[r["benchmark"]].append(r)
    samples = _samples_from_groups(groups, metric, statistic)
    ctx = {k: rep_row[k] for k in ROW_STAMP_FIELDS if k in rep_row}
    return _normalize_samples(samples, key, str(path)), ctx


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """A summary of one session in a result file.

    Attributes
    ----------
    id : str or None
        The session's time-ordered id.
    date : str or None
        When the session started, in ISO 8601.
    host : str or None
        The machine it ran on.
    tag : str or None
        Its ``--session-tag``.
    benchmarks : int
        Distinct benchmarks it measured, without aggregate and skipped rows.
    rows : int
        Rows it stored, of every kind.
    """

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

    Parameters
    ----------
    path : str or Path
        Result file to read: ``.json``, ``.jsonl``, or ``.jsonl.gz``.

    Returns
    -------
    list[SessionSummary]
        One summary per session; sessions with the same date are ordered by
        their time-ordered id.

    Raises
    ------
    ValueError
        If the file has another extension or is not a mew result file.
    OSError
        If the file can't be read; :class:`FileNotFoundError` if it doesn't exist.
    """
    by_session = _group_by_session(read_rows(Path(path)), measured_only=False)
    out: list[SessionSummary] = []
    for (date, sid, host), session_rows in reversed(by_session.items()):
        measured = {r["benchmark"] for r in session_rows if _is_measurement_row(r)}
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

    Raises
    ------
    ValueError
        If the file has another extension or is not a mew result file.
    OSError
        If the file can't be read; :class:`FileNotFoundError` if it doesn't exist.
    """
    return cast("list[BenchmarkResult]", read_rows(Path(path)))
