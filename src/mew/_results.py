"""Decode result files, normalize metadata, and select comparable session samples."""

from __future__ import annotations

import dataclasses
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

from mew._console import overflow
from mew._statistics import Statistic, reduce_statistic
from mew._typing import BenchmarkResult
from mew.reporter import _ROW_STAMP_FIELDS, canonical_name

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
    session_date : str or None
        Date of the session this sample came from, for provenance display.
    values : tuple[float, ...]
        Raw per-repetition values (same unit as ``value``), feeding the
        Mann-Whitney significance marker. A single repetition has one value.
    """

    name: str
    value: float
    stddev: float | None
    time_unit: str | None
    session_date: str | None
    values: tuple[float, ...] = ()

    @property
    def cv(self) -> float | None:
        """Coefficient of variation, or None without repetition data."""
        if self.stddev is None or not self.value:
            return None
        return self.stddev / abs(self.value)


@dataclass(frozen=True, slots=True)
class SessionData:
    """Measurements and context from one benchmark session."""

    key: tuple[str, str, str]
    context: dict[str, Any] = field(repr=False)
    samples: dict[str, Sample] = field(repr=False)
    session_tag: str | None = None

    @property
    def date(self) -> str | None:
        return self.key[0] or None

    @property
    def host(self) -> str | None:
        return self.key[1] or None

    @property
    def session_id(self) -> str | None:
        return self.key[2] or None

    @property
    def tag(self) -> str | None:
        """The session's label, if one was set with ``--session-tag``."""
        return self.session_tag

    @property
    def provenance(self) -> dict[str, Any]:
        """The session's ``context`` block: providers' values and the suite's own."""
        return self.context.get("context") or {}


def _is_aggregate_row(row: dict[str, Any]) -> bool:
    return bool(row.get("aggregate_name"))


def _is_measurement_row(row: dict[str, Any]) -> bool:
    """Return whether a row is a successful, non-aggregate measurement."""
    return (
        isinstance(row.get("name"), str) and not _is_aggregate_row(row) and not row.get("skipped")
    )


def _inherit_metadata(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Fill missing row stamps from the active file/segment header.

    A row's own fields win, including explicit empty values. Inheritance is
    resolved during decoding so later JSONL headers cannot affect earlier rows.
    """
    for key in _ROW_STAMP_FIELDS:
        if key not in row and context.get(key) is not None:
            row[key] = context[key]
    return row


def _rows_from_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON: {e}") from e
    benchmarks = doc.get("benchmarks") if isinstance(doc, dict) else None
    if not isinstance(benchmarks, list):
        raise ValueError(f"{path}: missing 'benchmarks' array")  # noqa: TRY004
    ctx = doc.get("context") or {}
    return [_inherit_metadata(row, ctx) for row in benchmarks], ctx


def _rows_from_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the JSONL sink (plain or gzip): one self-contained row per line.

    Current files are pure NDJSON; every row carries its session identity.
    Files from older versions (and worker channels that merge rows) interleave
    ``{"context": ...}`` header lines with rows; rows inherit their segment's
    identity for those, and ``file_ctx`` is the last segment's context.
    """
    rows: list[dict[str, Any]] = []
    file_ctx: dict[str, Any] = {}
    current: dict[str, Any] = {}
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
            if not isinstance(obj, dict):
                raise TypeError(f"{path}:{lineno}: expected a JSON object per line")
            if "name" in obj:
                rows.append(_inherit_metadata(obj, current))
            else:
                # Older archives have context headers; also accept a bare
                # context object (a line without a benchmark name).
                current = obj.get("context", obj) or {}
                file_ctx = current
    return rows, file_ctx


def _session_context(rep_row: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, Any]:
    """Combine legacy file properties with already-normalized row metadata."""
    ctx = {key: value for key, value in file_ctx.items() if key not in _ROW_STAMP_FIELDS}
    ctx.update({key: rep_row[key] for key in _ROW_STAMP_FIELDS if key in rep_row})
    return ctx


def _pivot_value(row: dict[str, Any], dimension: str) -> Any:
    """Read a dotted ``dimension`` from a row, such as ``context.vcs.commit``."""
    node: Any = row
    for part in dimension.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _session_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(date, host, id)`` for a result row."""
    sess = _session_block(row)
    return (str(sess.get("date") or ""), str(sess.get("host") or ""), str(sess.get("id") or ""))


def _session_block(row: dict[str, Any]) -> dict[str, Any]:
    """The normalized row's session block."""
    block = row.get("session") or {}
    return block if isinstance(block, dict) else {}


def _session_group(row: dict[str, Any]) -> tuple[str, str]:
    """Group runs on one host by tag, VCS commit, or session identity."""
    sess = _session_block(row)
    host = str(sess.get("host") or "")
    if tag := sess.get("tag"):
        return (host, f"tag:{tag}")
    commit = _pivot_value(row, "context.vcs.commit")
    if commit:
        return (host, f"commit:{commit}")
    return (host, f"id:{sess.get('id') or sess.get('date') or ''}")


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


def _read_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Dispatch on suffix to read ``(rows, file_ctx)`` from a result file.

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
    date: str | None,
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
            session_date=date,
            values=tuple(values),
        )
    return samples


def _group_by_session(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Group successful measurement rows by session."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if not _is_measurement_row(r):
            continue
        buckets.setdefault(_session_group(r), []).append(r)
    # A bucket may span several runs (same tag); it takes the identity of its
    # newest one, so `path@<id-prefix>` and chronological order still work.
    return {
        max(_session_key(r) for r in bucket_rows): bucket_rows for bucket_rows in buckets.values()
    }


def _group_by_name(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket rows by canonical ``name[label]``, the unit both load paths aggregate over."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(canonical_name(r["name"], r.get("label")), []).append(r)
    return groups


def _load_sessions(
    path: Path, metric: str, statistic: Statistic | None = None
) -> list[SessionData]:
    """Load all sessions in ascending date order."""
    rows, file_ctx = _read_rows(path)

    sessions: list[SessionData] = []
    # ISO-8601 dates sort lexicographically in chronological order.
    for skey, session_rows in sorted(_group_by_session(rows).items()):
        groups = _group_by_name(session_rows)
        samples = _samples_from_groups(groups, metric, skey[0] or None, statistic)
        first_group = next(iter(groups.values()), [])
        ctx = _session_context(first_group[0] if first_group else {}, file_ctx)
        sessions.append(
            SessionData(
                key=skey,
                context=ctx,
                samples=samples,
                session_tag=(ctx.get("session") or {}).get("tag"),
            )
        )
    return sessions


def _load_pivot_columns(
    path: Path, metric: str, key: str, dimension: str, statistic: Statistic | None = None
) -> list[tuple[str, dict[str, Sample], dict[str, Any]]]:
    """Pivot the latest session into ``(value, samples, context)`` columns."""
    rows, file_ctx = _read_rows(path)
    by_session = _group_by_session(rows)
    if not by_session:
        return []

    latest = max(by_session)  # date-leading key → most recent session
    by_value: dict[Any, list[dict[str, Any]]] = {}
    for r in by_session[latest]:
        by_value.setdefault(_pivot_value(r, dimension), []).append(r)

    columns: list[tuple[str, dict[str, Sample], dict[str, Any]]] = []
    for value, value_rows in by_value.items():
        if value is None:
            continue  # rows without the dimension; nothing to pivot
        groups = _group_by_name(value_rows)
        samples = _samples_from_groups(groups, metric, latest[0] or None, statistic)
        rep_row = next(iter(groups.values()))[0]
        ctx = _session_context(rep_row, file_ctx)
        samples = _normalize_samples(samples, key, f"{path}[{dimension}={value}]")
        columns.append((str(value), samples, ctx))
    return columns


def _select_latest(
    path: Path, sessions: list[SessionData]
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Select each benchmark's latest session, warning about discarded data."""
    if not sessions:
        return {}, {}
    merged: dict[str, Sample] = {}
    history: dict[str, list[SessionData]] = {}
    for session in sessions:  # ascending date; later sessions overwrite
        for name, sample in session.samples.items():
            merged[name] = sample
            history.setdefault(name, []).append(session)
    # One aggregated line: a long-lived --append archive would otherwise print
    # a near-identical warning per benchmark on every compare.
    stale = {name: owners for name, owners in history.items() if len(owners) > 1}
    if stale:
        preview = ", ".join(f"{n!r} ({len(o)} sessions)" for n, o in list(stale.items())[:3])
        extra = overflow(len(stale), 3)
        chosen = next(iter(stale.values()))[-1]
        print(
            f"warning: {path}: {len(stale)} benchmark(s) appear in multiple sessions; "
            f"keeping the latest per name, e.g. {preview}{extra} "
            f"(latest: date={chosen.key[0]!r}, host={chosen.key[1]!r})",
            file=sys.stderr,
        )
    return merged, sessions[-1].context


_ORDINAL_RE = re.compile(r"~(\d+)")
_MIN_ID_PREFIX = 4


def _split_selector(raw: str) -> tuple[Path, str | None]:
    """Split ``path@selector``, preserving existing paths that contain ``@``."""
    if Path(raw).exists():
        return Path(raw), None
    base, sep, selector = raw.rpartition("@")
    if not sep or not base:
        return Path(raw), None
    return Path(base), selector


def _resolve_session(path: Path, sessions: list[SessionData], selector: str) -> SessionData:
    """Resolve a ``path@selector`` to one session.

    Order: keywords (``latest``/``earliest``), ordinal (``~N``, N back from
    latest), exact ``session_tag``, then ``session_id`` prefix (≥4 chars).
    Ambiguous matches and misses are errors; explicit selection must be
    deterministic.
    """
    if not sessions:
        raise SystemExit(f"{path}: no sessions in file")
    if not selector:
        raise SystemExit(f"{path}: empty session selector after '@'")
    if selector == "latest":
        return sessions[-1]
    if selector == "earliest":
        return sessions[0]
    if m := _ORDINAL_RE.fullmatch(selector):
        n = int(m.group(1))
        if n >= len(sessions):
            raise SystemExit(f"{path}: @~{n} out of range ({len(sessions)} session(s) in file)")
        return sessions[-1 - n]  # ~0 == latest

    # One match per host: the group key is (host, tag), so a tag spanning two
    # hosts is two sessions and the selector cannot pick between them.
    tagged = [s for s in sessions if s.session_tag == selector]
    if len(tagged) == 1:
        return tagged[0]
    if len(tagged) > 1:
        hosts = sorted({s.host or "?" for s in tagged})
        raise SystemExit(
            f"{path}: session tag {selector!r} is ambiguous ({len(tagged)} sessions "
            f"on hosts {hosts}); select by session id instead"
        )

    if len(selector) >= _MIN_ID_PREFIX:
        pref = [s for s in sessions if s.session_id and s.session_id.startswith(selector)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            raise SystemExit(
                f"{path}: session id prefix {selector!r} is ambiguous ({len(pref)} matches)"
            )

    tags = sorted({s.session_tag for s in sessions if s.session_tag})
    ids = [s.session_id[:12] for s in sessions if s.session_id]
    hint = f" (tags: {tags}; ids: {ids})" if (tags or ids) else ""
    raise SystemExit(f"{path}: no session matching {selector!r}{hint}")


def _load(
    path: Path,
    metric: str,
    key: str = "name",
    selector: str | None = None,
    statistic: Statistic | None = None,
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Load a result file into one sample set: load → select → re-key.

    Without a selector, sessions merge latest-wins per name (warning on discards).
    A selector picks exactly one session, no merge.
    """
    sessions = _load_sessions(path, metric, statistic)
    if selector is None:
        samples, ctx = _select_latest(path, sessions)
    else:
        chosen = _resolve_session(path, sessions, selector)
        samples, ctx = chosen.samples, chosen.context
    return _normalize_samples(samples, key, str(path)), ctx


def read_results(path: str | Path) -> list[BenchmarkResult]:
    """Read result rows in file order with inherited metadata filled in.

    Accepts JSON, JSONL, and gzip-compressed results. File-level session and
    context fields are copied onto each row.

    Parameters
    ----------
    path : str or Path
        Result file to read.

    Returns
    -------
    list[BenchmarkResult]
        Stored rows, including aggregate and skipped rows.
    """
    rows, _ = _read_rows(Path(path))
    return cast("list[BenchmarkResult]", rows)


def read_sessions(
    path: str | Path,
    *,
    metric: str = "real_time",
    statistic: Statistic | None = None,
) -> list[SessionData]:
    """Read a result file into comparable per-session samples, oldest first.

    Parameters
    ----------
    path : str or Path
        Result file to read.
    metric : str, default "real_time"
        Which measurement ``Sample.value`` reduces. ``Sample`` is metric-specific,
        so comparing two metrics means two calls.
    statistic : callable, optional
        Reducer over each benchmark's per-repetition values; defaults to the median.

    Returns
    -------
    list[SessionData]
        Sessions with aggregate and skipped rows removed and repetitions reduced.
    """
    if metric not in _METRICS:
        raise SystemExit(f"unknown metric {metric!r}; choose from {sorted(_METRICS)}")
    return _load_sessions(Path(path), metric, statistic)
