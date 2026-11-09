"""The result row format: encode values for rows, read result files, group their
rows by run, and reduce them to samples: their median and a significance test.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import zlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from mew._console import warn
from mew._typing import BenchmarkResult

# Stamped onto every row so each one stands alone, in JSON and JSONL alike.
ROW_STAMP_FIELDS = ("run", "context")

#: Context fields that describe the machine and the mew that ran a benchmark:
#: results that differ in them compare environments, not only code.
SKEW_FIELDS = ("host", "cpu_model", "num_cpus", "cpu_scaling_enabled", "mew")


#: The result format this mew writes and reads, on every row of a result file.
#: Bumped only for changes that break readers; added fields don't bump it.
SCHEMA = "v1"


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
METRICS = TIME_METRICS | MEMORY_METRICS


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
        The median of the repetitions. Times are in nanoseconds, whatever
        unit the file stored them in.
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

        ``None`` without repetitions or for a zero mean.
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
        raise ValueError(f"{where}: not a mew result row")  # ruff: ignore[TRY004]
    if (schema := obj.get("schema")) != SCHEMA:
        found = "no schema" if schema is None else f"unsupported schema {schema!r}"
        raise ValueError(f"{where}: {found} (this mew reads {SCHEMA!r})")


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


def _run_key(row: dict[str, Any]) -> tuple[str, str]:
    """Return ``(date, id)`` for a result row.

    Sorts oldest first: the fixed-width ISO-8601 date leads, and the
    time-ordered UUIDv7 id breaks ties between equal dates.
    """
    run = _run_block(row)
    return (str(run.get("date") or ""), str(run.get("id") or ""))


def _run_block(row: dict[str, Any]) -> dict[str, Any]:
    """The row's ``run`` block, or ``{}``."""
    block = row.get("run") or {}
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


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Read a result file's rows.

    Errors carry a one-line message naming the file: :class:`ValueError` for an
    unsupported extension or malformed contents, :class:`OSError` (of the
    original subclass) if the file can't be read.
    """
    fmt = result_format(path.name)
    if fmt is None:
        raise ValueError(f"{path}: not a .json, .jsonl or .jsonl.gz file")
    reader = _rows_from_json if fmt == "json" else _rows_from_jsonl
    try:
        return reader(path)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"no such file or directory: {path}") from e
    except OSError as e:
        reason = e.strerror.lower() if e.strerror else f"cannot read ({e})"
        raise type(e)(f"{reason}: {path}") from e
    # A truncated or corrupt archive, or a file that isn't UTF-8 text.
    except (EOFError, zlib.error, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read {path}: {e}") from e


def _samples_from_groups(
    groups: dict[str, list[dict[str, Any]]],
    metric: str,
) -> dict[str, Sample]:
    """Reduce per-name row groups to :class:`Sample`s, each centered on its median."""
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
        samples[name] = Sample(
            value=float(statistics.median(values)), values=tuple(values), counter_format=fmt
        )
    return samples


def _group_by_run(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group rows by run, oldest key first.

    ISO-8601 dates lead the key, so it sorts chronologically. Every row counts,
    skipped ones included, so ``mew show``, ``mew log`` and ``mew compare`` see
    the same runs.
    """
    buckets: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[_run_key(r)].append(r)
    return dict(sorted(buckets.items()))


def _pooled_rows(path: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All of a file's rows, one sample however many runs appended them.

    Warns on stderr when the runs came from different machines, interpreters,
    mew versions or commits: their repetitions would mix environments.
    """
    if not rows:
        raise ValueError(f"{path}: no measurements in file")
    runs = list(_group_by_run(rows).values())
    if len(runs) > 1:
        for field in (*SKEW_FIELDS, "python", "commit"):
            values = {_shown(_skew_value(r[0], field)) for r in runs}
            if len(values) > 1:
                warn(f"{path}: pooled runs differ in {field}: {', '.join(sorted(values))}")
    return rows


def _shown(value: Any) -> str:
    """A context value as a warning shows it: a string as is, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _skew_value(row: dict[str, Any], field: str) -> Any:
    """A context value pooled runs should share; ``commit`` is the VCS commit."""
    context = row.get("context") or {}
    if field == "commit":
        vcs = context.get("vcs")
        return vcs.get("commit") if isinstance(vcs, dict) else None
    return context.get(field)


def load_column(
    path: Path,
    metric: str,
) -> tuple[dict[str, Sample], dict[str, Any], dict[str, str]]:
    """Load one comparison column from a result file: every run in it, reduced.

    Returns the samples by name, the provenance of the newest measured row, and
    the benchmarks the file has only as skipped, by name, with their reasons, so
    a caller can tell a skipped benchmark from a missing one.
    """
    selected = _pooled_rows(path, read_rows(path))
    measured = [r for r in selected if _is_measurement_row(r)]
    if not measured:
        raise ValueError(f"{path}: no measurements, only skipped rows")
    # Repetitions of one benchmark share its `name[label]`.
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in measured:
        groups[r["name"]].append(r)
    try:
        samples = _samples_from_groups(groups, metric)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e
    skipped = {
        r["name"]: r["skip"].get("message") or r["skip"].get("reason", "skipped")
        for r in selected
        if isinstance(r.get("skip"), dict) and r["name"] not in groups
    }
    provenance = _provenance(measured)
    # The newest run speaks for a file's runs, so say how many there were.
    if (runs := len({_run_key(r) for r in measured})) > 1:
        provenance["runs"] = runs
    return samples, provenance, skipped


def load_runs(path: Path) -> list[tuple[list[BenchmarkResult], dict[str, Any]]]:
    """Load each run in a result file, oldest first, as ``mew run`` reported it.

    Skipped rows stay, since the run reported them too.

    Returns
    -------
    list[tuple[list[BenchmarkResult], dict]]
        Per run, its rows in file order and their provenance (``run`` and
        ``context``).
    """
    rows = read_rows(path)
    if not rows:
        raise ValueError(f"{path}: no measurements in file")
    return [
        (cast("list[BenchmarkResult]", run), _provenance(run))
        for run in _group_by_run(rows).values()
    ]


def _provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``run`` and ``context`` of the newest row, which speaks for all of them."""
    newest = max(rows, key=_run_key)
    return {k: newest[k] for k in ROW_STAMP_FIELDS if k in newest}


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A summary of one run in a result file.

    Attributes
    ----------
    id : str or None
        The run's time-ordered id.
    date : str or None
        When the run started, in ISO 8601.
    name : str or None
        Its ``--run-name``.
    benchmarks : int
        Distinct benchmarks it measured, without skipped rows.
    rows : int
        Rows it stored, of every kind.
    """

    id: str | None
    date: str | None
    name: str | None
    benchmarks: int
    rows: int


def run_summaries(path: str | Path) -> list[RunSummary]:
    """Summarize every run in a result file, newest first, as ``mew log`` lists them.

    ``rows`` counts every stored row of the run, and ``benchmarks`` the
    distinct measured benchmarks, without skipped rows.

    Parameters
    ----------
    path : str or Path
        Result file to read: ``.json``, ``.jsonl``, or ``.jsonl.gz``.

    Returns
    -------
    list[RunSummary]
        One summary per run. Runs with the same date are ordered by their
        time-ordered id.

    Raises
    ------
    ValueError
        If the file has another extension or is not a mew result file.
    OSError
        If the file can't be read, :class:`FileNotFoundError` if it doesn't exist.
    """
    by_run = _group_by_run(read_rows(Path(path)))
    out: list[RunSummary] = []
    for (date, run_id), run_rows in reversed(by_run.items()):
        measured = {r["name"] for r in run_rows if _is_measurement_row(r)}
        out.append(
            RunSummary(
                id=run_id or None,
                date=date or None,
                name=_run_block(run_rows[0]).get("name"),
                benchmarks=len(measured),
                rows=len(run_rows),
            )
        )
    return out


def read_results(path: str | Path) -> list[BenchmarkResult]:
    """Read result rows in file order.

    Accepts JSON, JSONL, and gzip-compressed results. Every row carries its own
    ``run`` and ``context``.

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
        If the file can't be read, :class:`FileNotFoundError` if it doesn't exist.
    """
    return cast("list[BenchmarkResult]", read_rows(Path(path)))


def _average_ranks(values: list[float]) -> tuple[list[float], float]:
    """Rank ``values`` ascending (1-based), averaging ties.

    Returns ``(ranks, tie_term)``: ``tie_term`` is ``sum(t**3 - t)`` per tie
    group of size ``t``, the variance correction term.
    """
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    tie_term = 0.0
    next_rank = 1
    for _, group in groupby(order, key=values.__getitem__):
        tied = list(group)
        count = len(tied)
        avg_rank = next_rank + (count - 1) / 2
        for i in tied:
            ranks[i] = avg_rank
        tie_term += count**3 - count
        next_rank += count
    return ranks, tie_term


#: Both samples at most this size and no ties: the exact null distribution, at
#: most 20 polynomial steps of 401 terms. Past it the normal approximation is
#: close, and exact tables would grow with every comparison row.
_EXACT_MAX = 20


def _u_counts(n1: int, n2: int) -> list[int]:
    """How many of the ``C(n1 + n2, n1)`` rank orders give each U, from 0 to ``n1 * n2``.

    The counts are the coefficients of the Gaussian binomial
    ``[n1 + n2 choose n1]_q``, built as the product over ``i`` of
    ``(1 - q**(n2 + i)) / (1 - q**i)``; every partial product is a polynomial.
    """
    poly = [1]
    for i in range(1, n1 + 1):
        shift = n2 + i
        poly += [0] * shift
        for j in range(len(poly) - 1, shift - 1, -1):  # times (1 - q**shift)
            poly[j] -= poly[j - shift]
        for j in range(i, len(poly)):  # divided by (1 - q**i)
            poly[j] += poly[j - i]
        del poly[i * n2 + 1 :]  # the quotient's degree; the rest is zero
    return poly


def mannwhitney_p(a: list[float], b: list[float]) -> float:
    """Return a two-sided p-value; compare passes two or more values per sample.

    Without ties and with at most ``_EXACT_MAX`` values per sample, the p-value
    is exact: the share of rank orders at least as extreme as the observed one.
    Otherwise it is the normal approximation with tie and continuity corrections.
    """
    n1, n2 = len(a), len(b)
    ranks, tie_term = _average_ranks([*a, *b])
    u1 = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    if not tie_term and max(n1, n2) <= _EXACT_MAX:
        counts = _u_counts(n1, n2)
        # Without ties U is a whole number, and its distribution is symmetric.
        tail = sum(counts[: round(min(u1, n1 * n2 - u1)) + 1])
        return min(1.0, 2 * tail / sum(counts))
    n = n1 + n2
    mu = n1 * n2 / 2
    sigma2 = (n1 * n2 / 12) * ((n + 1) - tie_term / (n * (n - 1)))
    if sigma2 <= 0:
        # No spread across combined ranks: no evidence of a difference, i.e. p=1.0.
        return 1.0
    sigma = math.sqrt(sigma2)
    diff = u1 - mu
    correction = 0.5 if diff > 0 else -0.5 if diff < 0 else 0.0
    z = (diff - correction) / sigma
    # 2 * (1 - Phi(|z|)), via erfc to stay accurate for tiny p.
    return math.erfc(abs(z) / math.sqrt(2))


_SCALARS = (str, int, float, bool, type(None))


def type_name(value: Any) -> str:
    """``value``'s type as ``module.QualName``, e.g. ``"torch.Tensor"``."""
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def encode_param(value: Any) -> Any:
    """A case parameter as a row holds it: a JSON scalar, or its type's name."""
    return value if isinstance(value, _SCALARS) else type_name(value)


def encode_context(value: Any) -> Any:
    """A context value as a row holds it.

    Scalars stay; mappings and lists or tuples are encoded item by item, a
    mapping's keys as strings; a path becomes its ``os.fspath``; anything else
    its type's qualified name.
    """
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        return {_key(k): encode_context(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [encode_context(v) for v in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return type_name(value)


def _key(key: Any) -> str:
    """A mapping key as JSON needs it, a string: as JSON itself writes a number key."""
    if isinstance(key, str):
        return key
    return str(key) if isinstance(key, _SCALARS) else type_name(key)
