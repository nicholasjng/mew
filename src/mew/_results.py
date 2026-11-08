"""Read result files, group their rows by session, and reduce them to samples."""

from __future__ import annotations

import json
import statistics
import zlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from mew._format import overflow
from mew._statistics import Statistic
from mew._typing import BenchmarkResult

# Stamped onto every row so each one stands alone, in JSON and JSONL alike.
ROW_STAMP_FIELDS = ("session", "context")

#: The result format this mew writes and reads, on every row of a result file.
#: Bumped only for changes that break readers; added fields don't bump it.
SCHEMA_VERSION = 1


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

        return cast(TextIO, gzip.open(path, mode + "t", encoding="utf-8"))
    # JSON is UTF-8 whatever the locale, as on Windows.
    return cast(TextIO, path.open(mode, encoding="utf-8"))


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


def counter_name(metric: str) -> str | None:
    """The counter a ``counters.<name>`` metric names; ``None`` for any other metric.

    ``<name>`` may itself contain dots.
    """
    head, _, tail = metric.partition(".")
    return tail if head == "counters" and tail else None


@dataclass(frozen=True, slots=True)
class Sample:
    """One benchmark's measurement in one file, reduced over its repetitions.

    Attributes
    ----------
    value : float
        The statistic over the repetitions, the median by default. Times are
        in nanoseconds, whatever unit the file stored them in.
    values : tuple[float, ...]
        The per-repetition values, in ``value``'s unit, for the significance test.
    counter_format : dict or None
        For a ``counters.<name>`` metric, the counter's ``format`` record from
        the benchmark's first row that has the counter, if any; ``None`` for a
        plain counter and for other metrics.
    """

    value: float
    values: tuple[float, ...] = ()
    counter_format: dict[str, Any] | None = None

    @property
    def stddev(self) -> float | None:
        """The sample stddev of ``values``; ``None`` for a single repetition."""
        return statistics.stdev(self.values) if len(self.values) > 1 else None

    @property
    def cv(self) -> float | None:
        """The coefficient of variation, ``stddev`` over the mean of ``values``.

        Uses the mean whatever statistic ``value`` holds, so the choice of
        statistic does not move the noise marker. ``None`` without repetitions
        or for a zero mean.
        """
        if (stddev := self.stddev) is None:
            return None
        mean = statistics.fmean(self.values)
        return stddev / abs(mean) if mean else None


def _is_measurement_row(row: dict[str, Any]) -> bool:
    """Return whether a row is a measurement, not a skipped benchmark."""
    return isinstance(row.get("name"), str) and not row.get("skip")


def _check_row(obj: Any, where: str) -> None:
    """Reject anything but a result row of this mew's format."""
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        raise ValueError(f"{where}: expected a mew result row (a JSON object with a 'name')")  # ruff: ignore[TRY004]
    if (version := obj.get("schema_version")) != SCHEMA_VERSION:
        written = "no schema_version" if version is None else f"schema_version {version!r}"
        raise ValueError(
            f"{where}: written in another result format ({written}); this mew reads "
            f"schema_version {SCHEMA_VERSION}. Re-run the benchmarks to compare them"
        )


def _rows_from_json(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
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


def _session_key(row: dict[str, Any]) -> tuple[str, str]:
    """Return ``(date, id)`` for a result row.

    Sorts oldest first: the ISO-8601 date leads, and the time-ordered UUIDv7 id
    breaks ties between equal dates.
    """
    sess = _session_block(row)
    return (str(sess.get("date") or ""), str(sess.get("id") or ""))


def _session_block(row: dict[str, Any]) -> dict[str, Any]:
    """The normalized row's session block."""
    block = row.get("session") or {}
    return block if isinstance(block, dict) else {}


def _counter(row: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The row's counter record named ``name``, if it has one."""
    counters = row.get("counters")
    if isinstance(counters, list):
        return next((c for c in counters if isinstance(c, dict) and c.get("name") == name), None)
    return None


def _metric_value(row: dict[str, Any], metric: str) -> Any:
    """Look up ``metric`` in a row; dotted metrics traverse one nested level.

    Counters are a list of ``{"name", "value"}`` records, so a counter is found
    by name; ``memory`` is a mapping.
    """
    head, sep, tail = metric.partition(".")
    value = row.get(head)
    if not sep:
        return value
    if head == "counters":
        counter = _counter(row, tail)
        return counter.get("value") if counter is not None else None
    return value.get(tail) if isinstance(value, dict) else None


def _metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    """Per-repetition values, dropping absent metrics; rows hold times in nanoseconds."""
    return [_number(v, r, metric) for r in rows if (v := _metric_value(r, metric)) is not None]


def _number(value: Any, row: dict[str, Any], metric: str) -> float:
    """``value`` as a float, or a :class:`ValueError` naming the row and metric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{row.get('name')!r}: {metric} is {value!r}, not a number") from None


def _normalize_samples(samples: dict[str, Sample], key: str, source: str) -> dict[str, Sample]:
    """Re-key samples for the requested match key, erroring on collisions.

    ``key="func"`` strips the ``file.py::`` prefix from each registered name.
    """
    if key == "name":
        return samples
    origin: dict[str, str] = {}  # short name -> the full name it came from
    for full in samples:
        short = full.rsplit("::", 1)[-1]
        if (first := origin.setdefault(short, full)) != full:
            raise ValueError(
                f"{source}: --key {key} maps both {first!r} and {full!r} "
                f"to {short!r}; disambiguate or use the default --key name"
            )
    return {short: samples[full] for short, full in origin.items()}


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
    # A truncated or corrupt archive, or a file that isn't UTF-8 text.
    except (EOFError, zlib.error, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read result file {path}: {e}") from e


def _samples_from_groups(
    groups: dict[str, list[dict[str, Any]]],
    metric: str,
    statistic: Statistic | None = None,
) -> dict[str, Sample]:
    """Aggregate per-name row groups into center/stddev :class:`Sample`s."""
    samples: dict[str, Sample] = {}
    counter = counter_name(metric)
    for name, group in groups.items():
        values = _metric_values(group, metric)
        if not values:
            continue
        fmt = None
        if counter is not None:
            # From the first row that has the counter: flags don't change between repetitions.
            fmt = next((r.get("format") for g in group if (r := _counter(g, counter))), None)
        try:
            # A user reducer may return e.g. a numpy scalar.
            center = float((statistic or statistics.median)(values))
        except statistics.StatisticsError as e:
            # E.g. gmean over a zero before Python 3.13; fail rather than drop the benchmark.
            raise ValueError(f"statistic failed on {name!r}: {e}") from e
        samples[name] = Sample(value=center, values=tuple(values), counter_format=fmt)
    return samples


def _group_by_session(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group rows by session, oldest key first.

    ISO-8601 dates lead the key, so it sorts chronologically. Every row counts,
    skipped ones included, so a selector names the same session in ``mew show``,
    ``mew sessions`` and ``mew compare``.
    """
    buckets: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
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
    by_session: dict[tuple[str, str], list[dict[str, Any]]],
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
    # Selected before filtering, so `@latest` is the session `mew show` prints
    # even when all of it was skipped.
    selected = _select_rows(path, _group_by_session(reader(path)), selector)
    measured = [r for r in selected if _is_measurement_row(r)]
    if not measured:
        which = "the latest session" if selector in (None, "latest") else f"session {selector!r}"
        raise ValueError(f"{path}: {which} has no measurements, only skipped rows")
    # Repetitions of one benchmark share its `name[label]`.
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in measured:
        groups[r["name"]].append(r)
    try:
        samples = _samples_from_groups(groups, metric, statistic)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e
    return _normalize_samples(samples, key, str(path)), _provenance(measured)


def load_session(
    path: Path, selector: str | None = None
) -> tuple[list[BenchmarkResult], dict[str, Any]]:
    """Load the rows a ``path@selector`` names, as ``mew run`` reported them.

    Skipped rows stay, since the run reported them too.

    Returns
    -------
    tuple[list[BenchmarkResult], dict]
        The rows in file order, and the provenance (``session`` and
        ``context``) of the newest one.
    """
    selected = _select_rows(path, _group_by_session(read_rows(path)), selector)
    return cast("list[BenchmarkResult]", selected), _provenance(selected)


def _provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``session`` and ``context`` of the newest row, which speaks for all of them."""
    newest = max(rows, key=_session_key)
    return {k: newest[k] for k in ROW_STAMP_FIELDS if k in newest}


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """A summary of one session in a result file.

    Attributes
    ----------
    id : str or None
        The session's time-ordered id.
    date : str or None
        When the session started, in ISO 8601.
    tag : str or None
        Its ``--session-tag``.
    benchmarks : int
        Distinct benchmarks it measured, without skipped rows.
    rows : int
        Rows it stored, of every kind.
    """

    id: str | None
    date: str | None
    tag: str | None
    benchmarks: int
    rows: int


def session_summaries(path: str | Path) -> list[SessionSummary]:
    """Summarize every session in a result file, newest first.

    ``rows`` counts every stored row of the session; ``benchmarks`` counts
    distinct measured benchmarks (skipped rows excluded).

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
    by_session = _group_by_session(read_rows(Path(path)))
    out: list[SessionSummary] = []
    for (date, sid), session_rows in reversed(by_session.items()):
        measured = {r["name"] for r in session_rows if _is_measurement_row(r)}
        out.append(
            SessionSummary(
                id=sid or None,
                date=date or None,
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
        Stored rows, including skipped rows.

    Raises
    ------
    ValueError
        If the file has another extension or is not a mew result file.
    OSError
        If the file can't be read; :class:`FileNotFoundError` if it doesn't exist.
    """
    return cast("list[BenchmarkResult]", read_rows(Path(path)))
