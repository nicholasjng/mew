"""Compare benchmark result files: deltas, speedups, optional stddev.

Structured as three stages so future features feed the same renderer:

1. **Load** (:func:`_load_sessions`): read a result file into per-session
   sample groups, discarding nothing.
2. **Select** (:func:`_select_latest`): resolve the groups to one sample set
   per file — today always "latest session per name, with a warning"; session
   selectors (``path@tag``) will slot in here.
3. **Render** (:func:`_render`): compare a list of labelled columns. File
   comparisons produce one column per file; a variant pivot would produce one
   column per variant from a single file.
"""

from __future__ import annotations

import dataclasses
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from mew._console import Span, Table, Terminal, sgr
from mew._registry import compile_name_filter
from mew._statistics import Statistic, reduce_statistic
from mew.regressions import BenchmarkVerdict, RegressionConfig, report
from mew.reporter import _fmt_bytes, canonical_name

_MEMORY_METRICS = frozenset(
    {
        "memory.peak_bytes",
        "memory.total_bytes",
        "memory.total_allocations",
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
    name: str
    value: float
    stddev: float | None
    time_unit: str | None
    session_date: str | None

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


def _decode_json_str(value: Any) -> Any:
    """Parse a JSON string into its value; pass non-strings through unchanged.

    Parquet stores nested blocks (``custom``, ``memory``) as JSON string columns.
    Returns ``None`` on malformed JSON so callers treat "absent" and "unparseable" alike.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _is_aggregate_row(row: dict[str, Any]) -> bool:
    return bool(row.get("aggregate_name"))


def _rows_from_json(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    doc = json.loads(path.read_text())
    benchmarks = doc.get("benchmarks")
    if not isinstance(benchmarks, list):
        raise ValueError(f"{path}: missing 'benchmarks' array")
    ctx = doc.get("context") or {}
    return benchmarks, ctx


# Identity fields a row inherits from its active JSONL context segment, so an
# appended (multi-session) file keys each row to the right session.
_SEGMENT_FIELDS = (
    "date",
    "host_name",
    "num_cpus",
    "cpu_scaling_enabled",
    "session_id",
    "session_tag",
)


def _rows_from_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the streaming sink: a ``{"context": ...}`` header line, then one row per line.

    A file written with ``mew run --append`` has several header/rows *segments*.
    Each row inherits its segment's identity (so rows land in the right session);
    ``file_ctx`` is the last segment's context, a single-block fallback.
    """
    rows: list[dict[str, Any]] = []
    file_ctx: dict[str, Any] = {}
    current: dict[str, Any] = {}
    # Stream line-by-line: a growing --append archive can be large;
    # read_text().splitlines() would hold the whole file plus a list of every
    # line in memory at once, on top of the parsed rows.
    with path.open() as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{lineno}: expected a JSON object per line")
            if "name" in obj:
                for fld in _SEGMENT_FIELDS:
                    obj.setdefault(fld, current.get(fld))
                if "custom" not in obj and current.get("custom") is not None:
                    obj["custom"] = current["custom"]
                rows.append(obj)
            else:
                # mew's own sink writes `{"context": {...}}` on line 1; also accept
                # a bare context object (any line without a benchmark name).
                current = obj.get("context", obj) or {}
                file_ctx = current
    return rows, file_ctx


# Columns every comparison needs regardless of metric. Notably excludes the
# `cpu_profile` JSON blob: expensive to decode per row, never read by a compare.
_PARQUET_BASE_COLUMNS = (
    "name",
    "label",
    "aggregate_name",
    "variant",
    "time_unit",
    *_SEGMENT_FIELDS,
    "custom",
)


def _parquet_projection(metric: str) -> list[str]:
    """Base columns plus the metric source: `memory` for a ``memory.*`` metric, else
    the flat column. The caller intersects with the file schema (schema evolution)."""
    cols = list(dict.fromkeys(_PARQUET_BASE_COLUMNS))  # de-dup (date etc. via _SEGMENT_FIELDS)
    head = metric.split(".", 1)[0]
    cols.append("memory" if head == "memory" else head)
    return cols


def _read_parquet(path: Path, metric: str, *, filters: Any = None) -> list[dict[str, Any]]:
    """Read metric-projected rows, ``filters`` (pyarrow DNF) pushed into the scan.

    Projection intersects with the file schema (so a missing column is skipped, not
    an error) and decodes only the JSON columns it kept (`custom`, `memory`);
    `cpu_profile` is never projected.
    """
    if find_spec("pyarrow") is None:
        raise SystemExit(
            "pyarrow is required to read Parquet result files. "
            "Install it with: uv add --optional parquet pyarrow"
        )
    import pyarrow.parquet as pq

    available = set(pq.read_schema(path).names)
    columns = [c for c in _parquet_projection(metric) if c in available]
    rows = pq.read_table(path, columns=columns, filters=filters).to_pylist()
    json_cols = [c for c in ("custom", "memory") if c in columns]
    for row in rows:
        for col in json_cols:
            if isinstance(row.get(col), str):
                row[col] = _decode_json_str(row[col])
    return rows


def _rows_from_parquet(path: Path, metric: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _read_parquet(path, metric)
    # Parquet has no file-level context block; rebuild one from the per-row
    # session columns of the first row (same projection every session row carries).
    return rows, _session_context(rows[0] if rows else {}, {})


def _session_context(rep_row: dict[str, Any], file_ctx: dict[str, Any]) -> dict[str, Any]:
    """Build one session's context from a representative row over the file block.

    Per-row identity (Parquet columns, enriched JSONL segments) wins; single-doc
    JSON has identity only in ``file_ctx``, so the block stands. ``custom`` arrives
    already decoded (see :func:`_read_rows`).
    """
    ctx = dict(file_ctx)
    for fld in _SEGMENT_FIELDS:
        v = rep_row.get(fld)
        if v is not None:
            ctx[fld] = v
    if custom := rep_row.get("custom"):
        ctx["custom"] = custom
    return ctx


def _session_key(row: dict[str, Any], file_ctx: dict[str, Any]) -> tuple[str, str, str]:
    """Identify which session a row belongs to.

    Parquet rows carry session columns per-row; JSON rows inherit the file's single
    top-level context block via ``file_ctx``. ``session_id`` keeps two runs distinct
    even when they share a wall-clock second on one host; files predating it fall back
    to (date, host). Date leads so chronological sort still holds.
    """
    date = row.get("date") or file_ctx.get("date") or ""
    host = row.get("host_name") or file_ctx.get("host_name") or ""
    sid = row.get("session_id") or file_ctx.get("session_id") or ""
    return (str(date), str(host), str(sid))


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


def _aggregate_group(
    rows: list[dict[str, Any]], metric: str, statistic: Statistic | None = None
) -> tuple[float, float | None]:
    """Center and (sample) stddev across a group of per-repetition rows.

    The center is the median by default (stdlib, no numpy); ``statistic`` swaps in
    a custom reducer (p95, geometric mean, …) via :func:`reduce_statistic`. stddev
    stays the spread measure either way, feeding the noise (CV) flag.
    """
    values = [float(v) for r in rows if (v := _metric_value(r, metric)) is not None]
    if not values:
        raise ValueError(f"no {metric!r} values in group")
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


def _read_rows(
    path: Path, metric: str = "real_time"
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Dispatch on suffix to read ``(rows, file_ctx)`` from a result file.

    ``metric`` lets the Parquet reader project to only the columns that metric needs;
    JSON/JSONL are already streaming and read every field regardless.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _rows_from_json(path)
    if suffix == ".jsonl":
        return _rows_from_jsonl(path)
    if suffix in (".parquet", ".pq"):
        return _rows_from_parquet(path, metric)
    raise SystemExit(f"unsupported result file: {path} (use .json, .jsonl, or .parquet)")


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
        except ValueError:
            continue
        samples[name] = Sample(
            name=name,
            value=center,
            stddev=stddev,
            time_unit=group[0].get("time_unit"),
            session_date=date,
        )
    return samples


def _group_by_session(
    rows: list[dict[str, Any]], file_ctx: dict[str, Any]
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Bucket benchmark rows by session key, dropping non-benchmark and GB aggregate rows.

    Shared front half of both load paths: :func:`_load_sessions` sub-groups each
    bucket by name, :func:`_load_variant_columns` keeps the latest bucket whole and
    pivots it by variant. Aggregate rows are dropped because we recompute statistics
    ourselves, so results are consistent whether a file used ``--repetitions=1`` or N.
    """
    by_session: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if not isinstance(r.get("name"), str) or _is_aggregate_row(r):
            continue
        by_session.setdefault(_session_key(r, file_ctx), []).append(r)
    return by_session


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
    rows, file_ctx = _read_rows(path, metric)

    sessions: list[SessionData] = []
    # ISO-8601 dates sort lexicographically in chronological order.
    for skey, session_rows in sorted(_group_by_session(rows, file_ctx).items()):
        groups = _group_by_name(session_rows)
        samples = _samples_from_groups(groups, metric, skey[0] or None, statistic)
        first_group = next(iter(groups.values()), [])
        ctx = _session_context(first_group[0] if first_group else {}, file_ctx)
        sessions.append(
            SessionData(key=skey, context=ctx, samples=samples, session_tag=ctx.get("session_tag"))
        )
    return sessions


def _load_variant_columns(
    path: Path, metric: str, key: str, statistic: Statistic | None = None
) -> list[tuple[str, dict[str, Sample], dict[str, Any]]]:
    """Pivot one file's latest session into ``(variant, samples, context)`` columns.

    Variants and sessions are orthogonal: pick the latest session (commonly a single
    ``--variant`` run), then group its rows by ``variant``. Variant order follows first
    encounter, which is the declared/baseline-first order the orchestrator writes.
    """
    rows, file_ctx = _read_rows(path, metric)
    by_session = _group_by_session(rows, file_ctx)
    if not by_session:
        return []

    latest = max(by_session)  # date-leading key → most recent session
    by_variant: dict[Any, list[dict[str, Any]]] = {}
    for r in by_session[latest]:
        by_variant.setdefault(r.get("variant"), []).append(r)

    columns: list[tuple[str, dict[str, Sample], dict[str, Any]]] = []
    for variant, variant_rows in by_variant.items():
        if variant is None:
            continue  # rows from a non-variant run; nothing to pivot
        groups = _group_by_name(variant_rows)
        samples = _samples_from_groups(groups, metric, latest[0] or None, statistic)
        rep_row = next(iter(groups.values()))[0]
        ctx = _session_context(rep_row, file_ctx)
        samples = _normalize_samples(samples, key, f"{path}[variant={variant}]")
        columns.append((str(variant), samples, ctx))
    return columns


def _select_latest(
    path: Path, sessions: list[SessionData]
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Default session selection: per name, the latest session that has it wins.

    Warns per benchmark when older sessions are discarded, so concatenated archives
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
    for name, owners in history.items():
        if len(owners) > 1:
            chosen = owners[-1]
            print(
                f"warning: {path}: {name!r} has {len(owners)} sessions, "
                f"keeping latest (date={chosen.key[0]!r}, host={chosen.key[1]!r})",
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
    Ambiguous tag/prefix matches and misses are errors — explicit selection
    must be deterministic.
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

    tagged = [s for s in sessions if s.session_tag == selector]
    if len(tagged) == 1:
        return tagged[0]
    if len(tagged) > 1:
        raise SystemExit(
            f"{path}: session tag {selector!r} is ambiguous ({len(tagged)} sessions); "
            "select by session id instead"
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


def _parquet_session_index(path: Path) -> list[SessionData] | None:
    """Enumerate sessions from identity columns only — a cheap pass so ``path@selector``
    resolves the target without reading every session's metric rows.

    Returns None when the fast path can't apply (not Parquet, no pyarrow, or a legacy
    file lacking ``session_id``); the caller then falls back to the full read.
    """
    if path.suffix.lower() not in (".parquet", ".pq") or find_spec("pyarrow") is None:
        return None
    import pyarrow.parquet as pq

    available = set(pq.read_schema(path).names)
    if "session_id" not in available:
        return None
    cols = [c for c in ("date", "host_name", "session_id", "session_tag") if c in available]
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in pq.read_table(path, columns=cols).to_pylist():
        by_key.setdefault(_session_key(r, {}), r)  # one representative row per session
    return [
        SessionData(
            key=skey,
            context=(ctx := _session_context(by_key[skey], {})),
            samples={},
            session_tag=ctx.get("session_tag"),
        )
        for skey in sorted(by_key)  # date-leading key → chronological, like _load_sessions
    ]


def _load_parquet_selected(
    path: Path,
    metric: str,
    index: list[SessionData],
    selector: str,
    statistic: Statistic | None = None,
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Resolve ``selector`` against the cheap index, then read only that session's rows."""
    chosen = _resolve_session(path, index, selector)
    rows = _read_parquet(path, metric, filters=[("session_id", "==", chosen.key[2])])
    samples = _samples_from_groups(_group_by_name(rows), metric, chosen.key[0] or None, statistic)
    return samples, _session_context(rows[0] if rows else {}, {})


def _load(
    path: Path,
    metric: str,
    key: str = "name",
    selector: str | None = None,
    statistic: Statistic | None = None,
) -> tuple[dict[str, Sample], dict[str, Any]]:
    """Load a result file into one sample set: load → select → re-key.

    Without a selector, sessions merge latest-wins per name (warning on discards).
    A selector picks exactly one session, no merge — and for Parquet, reads only
    that session's rows (cheap index pass resolves it first).
    """
    if selector is not None and (index := _parquet_session_index(path)) is not None:
        samples, ctx = _load_parquet_selected(path, metric, index, selector, statistic)
    else:
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
    """Flatten nested custom-context dicts to dotted keys for display/diffing."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{dotted}."))
        else:
            out[dotted] = v
    return out


def _ctx_summary(ctx: dict[str, Any]) -> str:
    """One provenance line per column: session, host, cpus, scaling, date, custom.*."""
    parts: list[str] = []
    if ctx.get("session_tag"):
        parts.append(f"session={ctx['session_tag']}")
    elif ctx.get("session_id"):
        parts.append(f"session={str(ctx['session_id'])[:12]}")
    if ctx.get("host_name"):
        parts.append(f"host={ctx['host_name']}")
    if ctx.get("num_cpus") is not None:
        parts.append(f"cpus={ctx['num_cpus']}")
    if ctx.get("cpu_scaling_enabled") is not None:
        parts.append(f"cpu_scaling={'on' if ctx['cpu_scaling_enabled'] else 'off'}")
    if ctx.get("date"):
        parts.append(f"date={str(ctx['date'])[:19]}")
    for k, v in _flatten(ctx.get("custom") or {}).items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


def _warn_context_skew(columns: list[_Column]) -> None:
    """Warn when machine-level context differs across columns (deltas then compare
    environments, not just code)."""
    for fld in _CTX_SKEW_FIELDS:
        values = {c.label: c.context.get(fld) for c in columns if c.context}
        if len({v for v in values.values() if v is not None}) > 1:
            detail = ", ".join(f"{label}: {v}" for label, v in values.items())
            print(
                f"warning: result files differ in {fld} ({detail}) — "
                "deltas may reflect the environment, not the code",
                file=sys.stderr,
            )


def _custom_diffs(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-column dict of the custom-context keys whose values differ across columns."""
    flats = [_flatten(ctx.get("custom") or {}) for ctx in contexts]
    all_keys: set[str] = set().union(*flats)
    differing = sorted(
        k
        for k in all_keys
        if len({json.dumps(f.get(k), sort_keys=True, default=str) for f in flats}) > 1
    )
    return [{k: f.get(k) for k in differing if k in f} for f in flats]


def _fmt_value(sample: Sample, metric: str) -> str:
    if metric == "memory.allocations_per_iteration":  # fractional per-call count
        return f"{sample.value:,.1f}"
    if metric in ("iterations", "memory.total_allocations"):
        return f"{int(sample.value):,}"
    if metric in _MEMORY_METRICS:  # remaining memory metrics are byte-valued
        return _fmt_bytes(int(sample.value))
    unit = sample.time_unit or ""
    return f"{sample.value:.2f} {unit}".rstrip()


def _fmt_delta(delta: float) -> tuple[str, str]:
    pct = delta * 100.0
    text = f"{pct:+.2f}%"
    style = "green" if delta < 0 else "red" if delta > 0 else ""
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
    """One comparison column: a result file today, a variant group later.

    ``source`` identifies the column in warnings (file path / variant name);
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
    (files, sessions, variant groups) compares the same way.
    """
    all_names: set[str] = set().union(*(c.samples.keys() for c in columns))
    if pattern is not None:
        all_names = {n for n in all_names if pattern.search(n)}

    shared = set.intersection(*(set(c.samples.keys()) for c in columns)) & all_names
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
        missing = all_names - set(c.samples.keys())
        if missing:
            preview = ", ".join(sorted(missing)[:5])
            extra = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
            print(
                f"warning: {c.source} missing {len(missing)} benchmark(s): {preview}{extra}",
                file=sys.stderr,
            )

    _warn_context_skew(columns)
    # Custom-context keys that differ (e.g. engine=duckdb 1.5.3) annotate the
    # column labels, so an apples-vs-oranges comparison documents itself.
    diffs = _custom_diffs([c.context for c in columns])
    labels = [
        f"{c.label} ({', '.join(f'{k}={v}' for k, v in diff.items())})" if diff else c.label
        for c, diff in zip(columns, diffs, strict=True)
    ]

    term = console or Terminal()
    for label, c in zip(labels, columns, strict=True):
        if c.context:
            term.print(sgr(f"{label}: {_ctx_summary(c.context)}", "dim", enabled=term.color))

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
    verdicts: list[BenchmarkVerdict] = []
    baseline = columns[0].samples

    for name in sorted(shared):
        base = baseline[name]
        row: list[Any] = [name, _value_cell(base, metric)]
        if show_stddev:
            row.append(f"{base.stddev:.2f}" if base.stddev is not None else "-")
        for idx, c in enumerate(columns[1:]):
            s = c.samples[name]
            delta = (s.value - base.value) / base.value if base.value else 0.0
            speedup = base.value / s.value if s.value else float("inf")
            delta_text, delta_style = _fmt_delta(delta)
            row.append(_value_cell(s, metric))
            row.append([(delta_text, delta_style)] if delta_style else delta_text)
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

    term.print(table)

    if regressions is not None:
        return report(verdicts, default_threshold_pct=regressions.default_threshold_pct)
    return 0


def _variant_columns(
    path: Path, metric: str, key: str, baseline: str | None, statistic: Statistic | None = None
) -> list[_Column]:
    """Build comparison columns by pivoting one file's variants, baseline first."""
    loaded = _load_variant_columns(path, metric, key, statistic)
    names = [v for v, _, _ in loaded]
    if not names:
        raise SystemExit(f"{path}: no 'variant' data to pivot; produce it with `mew run --variant`")
    if baseline is not None and baseline not in names:
        raise SystemExit(f"{path}: --baseline {baseline!r} not among variants {names}")
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

    The first file is the baseline; later files show their value plus percent delta and speedup against it.

    Parameters
    ----------
    files : list[Path]
        Result files (JSON, JSONL, or Parquet); the first is treated as the
        baseline. A ``path@selector`` argument picks one session from a
        multi-session file — ``@latest``/``@earliest``, ``@~N`` (N back from
        latest), an exact ``session_tag``, or a ``session_id`` prefix
        (≥4 chars). Repeat one file with two selectors to compare two of its
        sessions (``results.parquet@before results.parquet@after``).
    metric : str, default "real_time"
        Metric to compare.
        One of ``"real_time"``, ``"cpu_time"``, ``"iterations"``, or — for files
        produced with ``--profile-memory`` — ``"memory.peak_bytes"``,
        ``"memory.total_bytes"``, ``"memory.total_allocations"``, or
        ``"memory.allocations_per_iteration"`` (the per-call allocation count,
        comparable across engines regardless of speed).
    key : str, optional
        How benchmarks are matched across files: ``"name"`` uses the full
        registered name; ``"func"`` strips the ``file.py::`` prefix so suites
        in different files with matching function names line up (A/B suites).
        Defaults to ``"func"`` with ``by="variant"`` (each variant's rows keep
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
        Pivot dimension. ``"variant"`` compares the variants within a single
        ``--variant`` result file (one column each) instead of comparing files.
    baseline : str, optional
        With ``by="variant"``, which variant is the baseline (default: the
        first one written).
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
    # Variant columns share the file prefix, so they only align on the func name.
    if key is None:
        key = "func" if by == "variant" else "name"
    if key not in _KEYS:
        raise SystemExit(f"unknown key {key!r}; choose from {sorted(_KEYS)}")
    try:
        name_filter = compile_name_filter(pattern, literal=literal) if pattern else None
    except ValueError as e:
        raise SystemExit(str(e)) from e

    if by == "variant":
        if len(files) != 1:
            raise SystemExit("mew compare --by variant takes exactly one result file")
        columns = _variant_columns(files[0], metric, key, baseline, statistic)
    elif by is not None:
        raise SystemExit(f"unknown --by dimension {by!r}; only 'variant' is supported")
    else:
        if len(files) < 2:
            raise SystemExit("mew compare needs at least two result files")
        parsed = [_split_selector(str(p)) for p in files]
        paths = [p for p, _ in parsed]
        columns = []
        for path, selector in parsed:
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
