"""Compare benchmark result files: deltas, speedups, optional stddev.

Structured as three stages so new comparison dimensions feed the same renderer:

1. **Load** (:func:`_load_sessions`): read a result file into per-session
   sample groups, discarding nothing.
2. **Select** (:func:`_select_latest`, :func:`_resolve_session`): resolve the
   groups to one sample set per file, either by ``path@selector`` or by
   defaulting to the latest session per name.
3. **Render** (:func:`_render`): compare a list of labelled columns, one per
   file or (under ``--by``) one per value of a pivot dimension in a single file.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import statistics
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, cast

from mew._console import Span, Table, Terminal, overflow, sgr
from mew._registry import compile_name_filter
from mew._significance import mannwhitney_p
from mew._statistics import Statistic, reduce_statistic
from mew._typing import BenchmarkResult
from mew.regressions import BenchmarkVerdict, RegressionConfig, report
from mew.reporter import _ROW_STAMP_FIELDS, _fmt_bytes, canonical_name

# `memory.total_bytes` and `memory.total_allocations` stay in stored files but
# are not compare metrics: total_bytes duplicates peak_bytes, and
# total_allocations is not comparable across differing iteration counts
# (allocations_per_iteration is the comparable form).
_MEMORY_METRICS = frozenset(
    {
        "memory.peak_bytes",
        "memory.allocations_per_iteration",
    }
)
_METRICS = frozenset({"real_time", "cpu_time", "iterations"}) | _MEMORY_METRICS
_HIGHER_IS_BETTER = frozenset({"iterations"})
_KEYS = frozenset({"name", "func"})

# Coefficient of variation (stddev / median) above which a row is flagged
# as too noisy to trust.
_CV_UNRELIABLE = 0.25

# Context fields that make timings incomparable when they differ across files.
_CTX_SKEW_FIELDS = ("host_name", "num_cpus", "cpu_scaling_enabled")


@dataclass(frozen=True, slots=True)
class Sample:
    """One benchmark's reduced measurement, the unit every column compares.

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
        Mann-Whitney significance marker. Empty for a single repetition.
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
    """One session's worth of samples from a result file.

    ``key`` is ``(date, host, session_id)``; the id component is empty for
    files written before sessions were persisted, where ``(date, host)`` is
    the best identity available.
    """

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
    """A per-repetition benchmark measurement usable for statistics.

    Excludes non-benchmark rows, GB aggregate rows (we recompute statistics
    ourselves), and skipped rows (their zeroed timings would drag medians
    toward 0 and produce infinite speedups).
    """
    return (
        isinstance(row.get("name"), str) and not _is_aggregate_row(row) and not row.get("skipped")
    )


def _rows_from_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: invalid JSON: {e}") from e
    benchmarks = doc.get("benchmarks") if isinstance(doc, dict) else None
    if not isinstance(benchmarks, list):
        raise ValueError(f"{path}: missing 'benchmarks' array")  # noqa: TRY004
    ctx = doc.get("context") or {}
    return benchmarks, ctx


# Identity fields a row inherits from its active JSONL context segment, so an
# appended (multi-session) file keys each row to the right session.


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
                # Back-fill from the file block for rows carrying no stamp of
                # their own (single-doc JSON, and older archives).
                for fld in _ROW_STAMP_FIELDS:
                    if fld not in obj and current.get(fld) is not None:
                        obj[fld] = current[fld]
                rows.append(obj)
            else:
                # mew's own sink writes `{"context": {...}}` on line 1; also accept
                # a bare context object (any line without a benchmark name).
                current = obj.get("context", obj) or {}
                file_ctx = current
    return rows, file_ctx


def _session_context(rep_row: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, Any]:
    """Build one session's context from a representative row over the file block.

    Per-row identity (self-contained JSONL rows) wins; single-doc JSON has it
    only in ``file_ctx``, so the block stands.
    """
    ctx = dict(file_ctx)
    for fld in _ROW_STAMP_FIELDS:
        v = rep_row.get(fld)
        if v is not None:
            ctx[fld] = v
    return ctx


def _pivot_value(row: dict[str, Any], dimension: str) -> Any:
    """Read a dotted ``dimension`` from a row, such as ``context.vcs.commit``."""
    node: Any = row
    for part in dimension.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _session_key(row: dict[str, Any], file_ctx: dict[str, Any]) -> tuple[str, str, str]:
    """Identify which session a row belongs to.

    JSONL rows carry the `session` block per-row; JSON rows inherit the file's
    single one via ``file_ctx``. The id keeps two runs distinct even when they
    share a wall-clock second on one host. Date leads so chronological sort holds.
    """
    sess = _session_block(row, file_ctx)
    return (str(sess.get("date") or ""), str(sess.get("host") or ""), str(sess.get("id") or ""))


def _session_block(row: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, Any]:
    """The row's `session` block, falling back to the file's."""
    block = row.get("session") or file_ctx.get("session") or {}
    return block if isinstance(block, dict) else {}


def _session_group(row: dict[str, Any], file_ctx: dict[str, Any]) -> tuple[str, str]:
    """The bucket a row aggregates into, which is *not* its identity.

    Runs on one host sharing a ``session.tag``, or (absent one) the same
    ``context.vcs.commit``, are one bucket: repeated runs at one revision belong
    together, so an ``--append`` archive of interleaved A/B runs reduces over
    every repetition instead of keeping only the last. Record the commit with
    ``mew.update_context(mew.vcs_context())``. Runs with neither fall back to
    their own session id (or date), one bucket per run.
    """
    sess = _session_block(row, file_ctx)
    host = str(sess.get("host") or "")
    if tag := sess.get("tag"):
        return (host, f"tag:{tag}")
    commit = _pivot_value(row, "context.vcs.commit") or _pivot_value(file_ctx, "context.vcs.commit")
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


class _NoMetricValues(ValueError):
    """A row group carries no values for the requested metric (skip the benchmark).

    Distinct from ``ValueError`` so a failing custom statistic (e.g.
    ``statistics.StatisticsError``, a ``ValueError`` subclass) is surfaced to the
    user instead of silently dropping the benchmark.
    """


def _metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    """Raw per-repetition ``metric`` values across a row group, dropping absent ones."""
    return [float(v) for r in rows if (v := _metric_value(r, metric)) is not None]


def _aggregate_group(
    rows: list[dict[str, Any]], metric: str, statistic: Statistic | None = None
) -> tuple[float, float | None]:
    """Center and (sample) stddev across a group of per-repetition rows.

    The center is the median by default (stdlib, no numpy); ``statistic`` swaps in
    a custom reducer (p95, geometric mean, …) via :func:`reduce_statistic`. stddev
    stays the spread measure either way, feeding the noise (CV) flag.
    """
    values = _metric_values(rows, metric)
    if not values:
        raise _NoMetricValues(f"no {metric!r} values in group")
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
        try:
            center, stddev = _aggregate_group(group, metric, statistic)
        except _NoMetricValues:
            continue
        except statistics.StatisticsError as e:
            # e.g. `--statistic stdev` on a single-repetition file: fail loudly
            # instead of dropping every benchmark and reporting "no overlap".
            raise SystemExit(f"--statistic failed on {name!r}: {e}") from e
        samples[name] = Sample(
            name=name,
            value=center,
            stddev=stddev,
            time_unit=group[0].get("time_unit"),
            session_date=date,
            values=tuple(_metric_values(group, metric)),
        )
    return samples


def _group_by_session(
    rows: list[dict[str, Any]], file_ctx: dict[str, Any]
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Bucket benchmark rows by session key, keeping only measurement rows
    (see :func:`_is_measurement_row`).

    Shared front half of both load paths: :func:`_load_sessions` sub-groups each
    bucket by name, :func:`_load_pivot_columns` keeps the latest bucket whole and
    pivots it on a dimension. Aggregate rows are dropped because we recompute statistics
    ourselves, so results are consistent whether a file used ``--repetitions=1`` or N.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if not _is_measurement_row(r):
            continue
        buckets.setdefault(_session_group(r, file_ctx), []).append(r)
    # A bucket may span several runs (same tag); it takes the identity of its
    # newest one, so `path@<id-prefix>` and chronological order still work.
    return {
        max(_session_key(r, file_ctx) for r in bucket_rows): bucket_rows
        for bucket_rows in buckets.values()
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
    """Load every session in a result file, sorted by ascending date.

    Nothing is discarded here; collapsing to one sample set per file is the select
    stage's job (:func:`_select_latest` today, ``path@…`` selectors later).
    """
    rows, file_ctx = _read_rows(path)

    sessions: list[SessionData] = []
    # ISO-8601 dates sort lexicographically in chronological order.
    for skey, session_rows in sorted(_group_by_session(rows, file_ctx).items()):
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
    """Pivot one file's latest session into ``(value, samples, context)`` columns.

    The pivot dimension and sessions are orthogonal: pick the latest session, then
    group its rows by ``dimension`` (typically ``context.<key>``, set per suite with
    :func:`mew.set_context`). Column order follows first encounter, which is the
    order the rows were written in.
    """
    rows, file_ctx = _read_rows(path)
    by_session = _group_by_session(rows, file_ctx)
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
    """Default session selection: per name, the latest session that has it wins.

    Warns (once) when older sessions are discarded, so concatenated archives
    don't silently compare stale numbers.
    """
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
    """Split ``path@selector`` into its parts.

    A file existing on disk under the whole argument is an escape hatch for names
    containing ``@`` (returned as a plain path). Otherwise the part after the last
    ``@`` is the selector.
    """
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

    shared = all_names.intersection(*(c.samples.keys() for c in columns))
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
    is_time_metric = metric not in _MEMORY_METRICS and not higher_is_better
    verdicts: list[BenchmarkVerdict] = []
    baseline = columns[0].samples
    unit_skew: dict[str, tuple[Any, Any]] = {}

    for name in sorted(shared):
        base = baseline[name]
        row: list[Any] = [name, _value_cell(base, metric)]
        if show_stddev:
            row.append(_fmt_stddev(base, metric))
        # Time metrics compare in nanoseconds so a declared-unit mismatch
        # across files doesn't turn into a bogus delta.
        base_value = _to_ns(base.value, base.time_unit) if is_time_metric else base.value
        for idx, c in enumerate(columns[1:]):
            s = c.samples[name]
            if is_time_metric and base.time_unit != s.time_unit:
                unit_skew[name] = (base.time_unit, s.time_unit)
            s_value = _to_ns(s.value, s.time_unit) if is_time_metric else s.value
            if base_value:
                delta = (s_value - base_value) / base_value
            else:
                # A zero baseline must not mask a nonzero contender as +0.00%.
                delta = 0.0 if not s_value else float("inf")
            # "How much better is the contender": contender/baseline for
            # higher-is-better metrics, baseline/contender otherwise.
            num, den = (s_value, base_value) if higher_is_better else (base_value, s_value)
            speedup = num / den if den else float("inf")
            delta_text, delta_style = _fmt_delta(delta, higher_is_better=higher_is_better)
            delta_cell: str | list[Span] = (
                [(delta_text, delta_style)] if delta_style else delta_text
            )
            p = _significance_p(base, s, is_time_metric=is_time_metric)
            if p is not None and p < _SIGNIFICANCE_ALPHA:
                spans = delta_cell if isinstance(delta_cell, list) else [(delta_cell, None)]
                delta_cell = [*spans, (" (signif.)", "bold")]
            row.append(_value_cell(s, metric))
            row.append(delta_cell)
            row.append(_fmt_speedup(speedup))
            if show_stddev:
                row.append(_fmt_stddev(s, metric))
            # Gate only against the first non-baseline column; the rightmost
            # columns are informational in a multi-file comparison.
            if regressions is not None and idx == 0:
                verdicts.append(
                    regressions.evaluate(name, delta * 100.0, higher_is_better=higher_is_better)
                )
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
        If given, gate the second file against the baseline and append a regression panel.
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


def read_results(path: str | Path) -> list[BenchmarkResult]:
    """Read a result file into its rows, newest session last.

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
    rows, file_ctx = _read_rows(Path(path))
    # Single-document JSON keeps identity in one block rather than per row;
    # fold it in so a caller never has to know which shape it read.
    for row in rows:
        for fld in _ROW_STAMP_FIELDS:
            if fld not in row and file_ctx.get(fld) is not None:
                row[fld] = file_ctx[fld]
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
