"""The result row format: encode values for rows, read result files, group their
rows by run, and reduce them to samples with a statistic and a significance test.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import zlib
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, SupportsFloat, TextIO, cast

from mew._console import overflow
from mew._typing import BenchmarkResult

# Stamped onto every row so each one stands alone, in JSON and JSONL alike.
ROW_STAMP_FIELDS = ("run", "context")

#: Context fields that describe the machine and the mew that ran a benchmark:
#: results that differ in them compare environments, not only code.
SKEW_FIELDS = ("host", "cpu_model", "num_cpus", "cpu_scaling_enabled", "mew")

#: The selector that pools every run in a file.
POOL_ALL = "*"

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


def _group_by_run(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group rows by run, oldest key first.

    ISO-8601 dates lead the key, so it sorts chronologically. Every row counts,
    skipped ones included, so a selector names the same run in ``mew show``,
    ``mew log`` and ``mew compare``.
    """
    buckets: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        buckets[_run_key(r)].append(r)
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
    by_run: dict[tuple[str, str], list[dict[str, Any]]],
    selector: str | None,
) -> list[dict[str, Any]]:
    """Pick the rows of the run a ``path@selector`` names.

    ``None`` and ``latest`` select the newest run, and ``*`` pools every run in
    the file, as repetitions of one sample, with a warning on stderr when the
    runs came from different machines, interpreters or mew versions;
    :func:`mew.run` rejects both as run names. Any other selector is a run name,
    and selects the newest run of that name: a name never pools, since two runs
    may come from different machines. ``mew log`` lists what a file holds.
    """
    if not by_run:
        raise ValueError(f"{path}: no measurements in file")
    if selector is None or selector == "latest":
        return by_run[max(by_run)]
    if selector == POOL_ALL:
        _warn_pooled_skew(path, list(by_run.values()))
        return [r for rows in by_run.values() for r in rows]
    if not selector:
        raise ValueError(f"{path}: empty run name after '@'")
    named = [key for key, rows in by_run.items() if _run_block(rows[0]).get("name") == selector]
    if not named:
        names = sorted({n for rows in by_run.values() if (n := _run_block(rows[0]).get("name"))})
        shown = ", ".join(names[:5]) + overflow(len(names), 5)
        hint = f" (run names in file: {shown})" if names else " (no named runs in file)"
        raise ValueError(f"{path}: no run named {selector!r}{hint}; see `mew log {path}`")
    return by_run[max(named)]


def _warn_pooled_skew(path: Path, runs: list[list[dict[str, Any]]]) -> None:
    """Warn when the runs ``file@*`` pools differ in their machine or interpreter."""
    for field in (*SKEW_FIELDS, "python"):
        values = {json.dumps((r[0].get("context") or {}).get(field), sort_keys=True) for r in runs}
        if len(values) > 1:
            print(
                f"warning: {path}@{POOL_ALL} pools runs that differ in {field} "
                f"({', '.join(sorted(values))}); their repetitions mix environments",
                file=sys.stderr,
            )


def load_column(
    path: Path,
    metric: str,
    key: str = "name",
    selector: str | None = None,
    statistic: Statistic | None = None,
    reader: Callable[[Path], list[dict[str, Any]]] = read_rows,
) -> tuple[dict[str, Sample], dict[str, Any], dict[str, str]]:
    """Load one comparison column from a result file: read, select a run, re-key.

    Returns the samples by name, the provenance of the newest measured row, and
    the benchmarks the run skipped, by name, with their reasons, so a caller can
    tell a skipped benchmark from a missing one. ``reader`` lets a caller
    comparing several selectors of one file parse it once.
    """
    # Selected before filtering, so `@latest` is the run `mew show` prints
    # even when all of it was skipped.
    selected = _select_rows(path, _group_by_run(reader(path)), selector)
    measured = [r for r in selected if _is_measurement_row(r)]
    if not measured:
        which = "the latest run" if selector in (None, "latest") else f"run {selector!r}"
        raise ValueError(f"{path}: {which} has no measurements, only skipped rows")
    # Repetitions of one benchmark share its `name[label]`.
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in measured:
        groups[r["name"]].append(r)
    try:
        samples = _samples_from_groups(groups, metric, statistic)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e
    skipped = {
        (r["name"].rsplit("::", 1)[-1] if key == "func" else r["name"]): r["skip"].get("message")
        or r["skip"].get("reason", "skipped")
        for r in selected
        if isinstance(r.get("skip"), dict) and r["name"] not in groups
    }
    provenance = _provenance(measured)
    # `@*` pools runs; the newest speaks for them, so say how many there were.
    if (runs := len({_run_key(r) for r in measured})) > 1:
        provenance["runs"] = runs
    return _normalize_samples(samples, key, str(path)), provenance, skipped


def load_run(
    path: Path, selector: str | None = None
) -> tuple[list[BenchmarkResult], dict[str, Any]]:
    """Load the rows a ``path@selector`` names, as ``mew run`` reported them.

    Skipped rows stay, since the run reported them too.

    Returns
    -------
    tuple[list[BenchmarkResult], dict]
        The rows in file order, and their provenance (``run`` and ``context``).
    """
    selected = _select_rows(path, _group_by_run(read_rows(path)), selector)
    return cast("list[BenchmarkResult]", selected), _provenance(selected)


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

    ``rows`` counts every stored row of the run; ``benchmarks`` counts
    distinct measured benchmarks (skipped rows excluded).

    Parameters
    ----------
    path : str or Path
        Result file to read: ``.json``, ``.jsonl``, or ``.jsonl.gz``.

    Returns
    -------
    list[RunSummary]
        One summary per run; runs with the same date are ordered by their
        time-ordered id.

    Raises
    ------
    ValueError
        If the file has another extension or is not a mew result file.
    OSError
        If the file can't be read; :class:`FileNotFoundError` if it doesn't exist.
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

    Accepts JSON, JSONL, and gzip-compressed results; every row carries its own
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
        If the file can't be read; :class:`FileNotFoundError` if it doesn't exist.
    """
    return cast("list[BenchmarkResult]", read_rows(Path(path)))


# Any float-castable result, e.g. a numpy scalar; callers convert it with float().
Statistic = Callable[[list[float]], SupportsFloat]


_BUILTIN_STATISTICS: dict[str, Statistic] = {
    "min": min,
    "max": max,
    "mean": statistics.fmean,
    "median": statistics.median,
    "gmean": statistics.geometric_mean,
}


def resolve_statistic(spec: str) -> Statistic:
    """Resolve a statistic name to a reducer callable.

    ``min``, ``max``, ``mean``, ``median`` or ``gmean``; raises :class:`ValueError`
    for anything else. Percentiles are left out: over the few repetitions a run
    has, they only interpolate between its largest values.
    """
    if spec in _BUILTIN_STATISTICS:
        return _BUILTIN_STATISTICS[spec]
    raise ValueError(
        f"statistic {spec!r}: unknown name; choose from {', '.join(sorted(_BUILTIN_STATISTICS))}"
    )


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
