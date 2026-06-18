"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol, TextIO, cast, runtime_checkable

from mew._console import Terminal, sgr
from mew._core import Run
from mew._typing import RunRow


@runtime_checkable
class Reporter(Protocol):
    """Duck-typed reporter interface consumed by the C++ runner.

    ``report_context`` and ``report_runs`` are required; ``finalize`` is optional.
    All callbacks run on the main thread with the GIL held.

    Methods
    -------
    report_context(context)
        Called once before any runs with the C++ context dict.
        Returning ``False`` aborts the Google Benchmark run.
    report_runs(runs)
        Called one or more times with a list of :class:`~mew._typing.RunRow`
        dicts (runs projected from the C++ ``Run``).
    """

    def report_context(self, context: dict[str, Any], /) -> bool: ...
    def report_runs(self, runs: list[RunRow], /) -> None: ...


_COL_SEP = " │ "


def _fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


# Per-benchmark option suffixes GB appends to the name, e.g. `/min_time:0.200`.
# Anchored to the end (they always follow the function and args parts) so path
# segments in the registered name can't false-match.
_OPTION_SUFFIXES_RE = re.compile(
    r"(?:/(?:min_time:[^/]+|min_warmup_time:[^/]+|iterations:\d+|repeats:\d+"
    r"|manual_time|process_time|real_time|threads:\d+))+$"
)
_CASE_SUFFIX_RE = re.compile(r"/case:\d+$")


def canonical_name(name: str, label: Any) -> str:
    """Strip GB option suffixes and render a parametrize case by its human label.

    ``bench.py::f/case:0/min_time:0.200`` with label ``n=10000`` becomes
    ``bench.py::f[n=10000]``. Shared by :class:`RichReporter` and :mod:`mew.compare`
    so both show the same name; the stored ``name`` field stays the raw GB name
    (compare canonicalizes on read).
    """
    name = _OPTION_SUFFIXES_RE.sub("", name)
    if label and isinstance(label, str):
        stripped, n = _CASE_SUFFIX_RE.subn("", name)
        if n:
            return f"{stripped}[{label}]"
    return name


def _build_context(context: dict[str, Any]) -> dict[str, Any]:
    """Project the raw C++ context dict into the serialized ``context`` block.

    Shared by :class:`JSONReporter` and :class:`JSONLReporter`. ``date`` is
    stamped at call time.
    """
    ctx: dict[str, Any] = {
        "date": datetime.now(UTC).isoformat(),
        "host_name": context.get("host_name"),
        "executable": context.get("executable"),
        "num_cpus": context.get("num_cpus"),
        "mhz_per_cpu": context.get("mhz_per_cpu"),
        "cpu_scaling_enabled": context.get("cpu_scaling") == "enabled",
        "library_build_type": context.get("library_build_type"),
    }
    # Session identity is injected by mew.run; reporters driven directly
    # (or by GB) have none, leaving these keys absent.
    if session_id := context.get("session_id"):
        ctx["session_id"] = session_id
    if session_tag := context.get("session_tag"):
        ctx["session_tag"] = session_tag
    # Declared variant order (baseline first), set by the --variant orchestrator.
    if variants := context.get("variants"):
        ctx["variants"] = variants
    custom = context.get("custom")
    if custom:
        ctx["custom"] = custom
    return ctx


def _run_to_dict(r: Run) -> RunRow:
    """Project a C++ ``Run`` to a base :class:`~mew._typing.RunRow`.

    Overlay keys (``variant`` / ``custom`` / ``memory`` / ``cpu_profile``) are
    added downstream, not read off the ``Run``.
    """
    counters = r.counters  # hot path on C++ Run: each access rebuilds the dict
    return {
        "name": r.benchmark_name(),
        "run_name": str(r.run_name),
        "family_index": r.family_index,
        "per_family_instance_index": r.per_family_instance_index,
        "run_type": r.run_type.name,
        "aggregate_name": r.aggregate_name,
        "repetitions": r.repetitions,
        "repetition_index": r.repetition_index,
        "threads": r.threads,
        "iterations": r.iterations,
        "real_time": r.adjusted_real_time(),
        "cpu_time": r.adjusted_cpu_time(),
        "real_accumulated_time": r.real_accumulated_time,
        "cpu_accumulated_time": r.cpu_accumulated_time,
        "time_unit": r.time_unit.name,
        "label": r.report_label,
        "skipped": r.skipped,
        "skip_message": r.skip_message,
        "counters": counters if counters else {},
    }


# Closing `]}` of the streamed doc, rewritten over itself each flush so the file
# stays valid JSON.
_JSON_CLOSER = "\n  ]\n}\n"


def _indent_block(text: str, spaces: int) -> str:
    """Re-indent every line after the first by ``spaces`` (json.dumps indents from col 0)."""
    return text.replace("\n", "\n" + " " * spaces)


def _open_sink(output: Path | TextIO | None, mode: str = "w") -> tuple[TextIO, bool]:
    """Resolve ``output`` to ``(file, owns_it)``.

    A Path is opened (owned), ``None`` → stdout, a stream is used as-is. ``mode``
    applies only to a Path sink (``"a"`` appends a new segment).
    """
    if isinstance(output, Path):
        return cast(TextIO, output.open(mode)), True
    if output is None:
        return sys.stdout, False
    return output, False


def _close_sink(fh: TextIO | None, owns_fh: bool) -> None:
    """Close ``fh`` only if this reporter opened it."""
    if owns_fh and fh is not None:
        fh.close()


class JSONReporter:
    """Emit a single ``{"context": ..., "benchmarks": [...]}`` document, GB-style.

    To a **seekable** sink it streams: writes context + empty array up front, then
    each :meth:`report_runs` seeks over the closing ``]}`` and re-writes it, so the
    file stays valid JSON after every flush (survives Ctrl-C). This covers a file
    we opened and a stdout/stream redirected to a regular file. A non-seekable sink
    (a terminal, a pipe) buffers and writes once at :meth:`finalize`.

    The one exception is a sink we did **not** open, on **Windows**: a stdout pipe
    there can report ``seekable()`` yet not honor the seek, duplicating content
    (the rows land after an already-closed empty document). So a non-owned sink on
    Windows buffers regardless of its ``seekable()`` self-report.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A Path is opened and closed here; a stream is written directly;
        ``None`` writes to ``sys.stdout``.
    """

    def __init__(self, *, output: Path | TextIO | None = None) -> None:
        self._output = output
        self._context: dict[str, Any] = {}
        self._runs: list[RunRow] = []  # buffered (non-seekable) path only
        self._fh: TextIO | None = None
        self._owns_fh = False
        self._streaming = False
        # Byte offset of the closer, i.e. where the next row gets written.
        self._reopen_pos = 0
        self._first_row = True

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        self._fh, self._owns_fh = _open_sink(self._output)
        # Stream into any seekable sink (file, or stdout redirected to a file).
        # Exception: a non-owned sink on Windows; a stdout pipe there can claim
        # seekable() but mishandle the seek and duplicate content, so buffer it.
        trust_seek = self._owns_fh or sys.platform != "win32"
        self._streaming = trust_seek and self._fh.seekable()
        if self._streaming:
            # default=str: don't crash on Path/datetime; lossy by design.
            ctx = _indent_block(json.dumps(_build_context(context), indent=2, default=str), 2)
            self._fh.write('{\n  "context": ' + ctx + ',\n  "benchmarks": [')
            self._reopen_pos = self._fh.tell()
            self._fh.write(_JSON_CLOSER)
            self._fh.flush()
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        if not self._streaming:
            self._runs.extend(runs)
            return
        assert self._fh is not None  # report_context runs first, always
        # Overwrite the closer with rows, then re-close. Rows are >> the closer,
        # so no stale bytes; empty `runs` just rewrites it in place.
        self._fh.seek(self._reopen_pos)
        for row in runs:
            prefix = "" if self._first_row else ","
            self._first_row = False
            rendered = _indent_block(json.dumps(row, indent=2, default=str), 4)
            self._fh.write(f"{prefix}\n    {rendered}")
        self._reopen_pos = self._fh.tell()
        self._fh.write(_JSON_CLOSER)
        self._fh.flush()

    def finalize(self) -> None:
        if not self._streaming:
            # Non-seekable sink: emit the whole document in one shot.
            assert self._fh is not None
            doc = {
                "context": _build_context(self._context),
                "benchmarks": self._runs,
            }
            self._fh.write(json.dumps(doc, indent=2, default=str) + "\n")
        _close_sink(self._fh, self._owns_fh)


class JSONLReporter:
    """Stream one JSON object per Run, one per line, flushed as runs land.

    Append-only (works on pipes): a long suite leaves a growing, ``tail``-able file
    that survives interruption. Line 1 is a ``{"context": {...}}`` header; each later
    line is one benchmark record. Consumers skip line 1 / branch on ``context``.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A Path is opened and closed here; a stream is written directly;
        ``None`` writes to ``sys.stdout``.
    append : bool, default False
        Open a Path sink in append mode, adding this run as a new
        context-header + rows *segment* (one more session). Ignored for
        stream / stdout sinks.
    """

    def __init__(self, *, output: Path | TextIO | None = None, append: bool = False) -> None:
        self._output = output
        self._append = append
        self._fh: TextIO | None = None
        self._owns_fh = False

    def report_context(self, context: dict[str, Any]) -> bool:
        self._fh, self._owns_fh = _open_sink(self._output, "a" if self._append else "w")
        # Header first so provenance lands before any rows.
        self._fh.write(json.dumps({"context": _build_context(context)}, default=str) + "\n")
        self._fh.flush()
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for row in runs:
            self._fh.write(json.dumps(row, default=str) + "\n")
        self._fh.flush()

    def finalize(self) -> None:
        _close_sink(self._fh, self._owns_fh)


class RichReporter:
    """Stream one row per benchmark to a terminal as runs complete.

    The header prints before any results land, so optional-column flags are passed up front.

    Parameters
    ----------
    terminal : mew._console.Terminal, optional
        Terminal to print to. Defaults to a fresh one (stdout).
    show_memory : bool, default False
        Add ``Peak Mem`` / ``Total Alloc`` columns.
    show_cpu : bool, default False
        Add ``Samples`` / ``Hottest Frame`` columns.
    show_label : bool, default False
        Add a ``Label`` column (the parametrize case id). Pass for families, where
        the case is otherwise indistinguishable from the truncated name.
    show_variant : bool, default False
        Add a ``Variant`` column (the ``--variant`` name). Pass when rows from
        several variants stream into one table.
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
        show_label: bool = False,
        show_variant: bool = False,
    ) -> None:
        self._term = terminal or Terminal()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
        self._show_label = show_label
        self._show_variant = show_variant
        self._context: dict[str, Any] = {}
        self._widths: dict[str, int] = {}

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        self._print_banner()
        self._compute_widths()
        self._print_header()
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        for row in runs:
            self._print_row(row)

    def finalize(self) -> None:
        pass

    def _print_banner(self) -> None:
        c = self._context
        host = c.get("host_name") or "?"
        cpus = c.get("num_cpus", "?")
        mhz = c.get("mhz_per_cpu", 0) or 0
        scaling = c.get("cpu_scaling", "?")
        color = self._term.color

        def cy(v: object) -> str:
            return sgr(str(v), "cyan", enabled=color)

        self._term.print(
            f"{sgr('mew', 'bold', enabled=color)} {sgr('·', 'dim', enabled=color)} "
            f"host={cy(host)} cpus={cy(cpus)} @ {cy(f'{mhz:.0f}MHz')} scaling={cy(scaling)}"
        )

    def _compute_widths(self) -> None:
        fixed: dict[str, int] = {
            "iters": 12,
            "real": 14,
            "cpu": 14,
        }
        if self._show_variant:
            fixed["variant"] = 16
        if self._show_label:
            fixed["label"] = 20
        if self._show_memory:
            fixed["peak"] = 10
            fixed["alloc"] = 12
        if self._show_cpu:
            fixed["samples"] = 9
            fixed["hottest_frame"] = 30
        n_cols = len(fixed) + 1  # +1 for the name column
        spacing = (n_cols - 1) * len(_COL_SEP)
        # Name column takes whatever's left; floor at 30 even if it overflows.
        name_w = max(30, self._term.width - sum(fixed.values()) - spacing)
        self._widths = {"name": name_w, **fixed}

    def _print_header(self) -> None:
        w = self._widths
        cells = ["Benchmark".ljust(w["name"])]
        if self._show_variant:
            cells.append("Variant".ljust(w["variant"]))
        if self._show_label:
            cells.append("Label".ljust(w["label"]))
        cells += [
            "Iters".rjust(w["iters"]),
            "Real".rjust(w["real"]),
            "CPU".rjust(w["cpu"]),
        ]
        if self._show_memory:
            cells.append("Peak Mem".rjust(w["peak"]))
            cells.append("Total Alloc".rjust(w["alloc"]))
        if self._show_cpu:
            cells.append("Samples".rjust(w["samples"]))
            cells.append("Hottest Frame".ljust(w["hottest_frame"]))
        line = _COL_SEP.join(cells)
        color = self._term.color
        self._term.print(sgr(line, "bold", enabled=color))
        self._term.print(sgr("─" * len(line), "dim", enabled=color))

    def _print_row(self, row: RunRow) -> None:
        w = self._widths
        unit = row["time_unit"]
        label = row["label"]
        # Canonical `file.py::f[label]` form (no `/case:N/min_time:…` noise), so
        # the live table reads the same as `mew compare`.
        name = canonical_name(row["name"], label)
        # Left-ellipsize: keep the disambiguating function suffix / case:N tail.
        if len(name) > w["name"]:
            name = "…" + name[-(w["name"] - 1) :]

        # Skipped rows carry no timing; render the reason in place of the
        # numeric columns and dim the whole line so it reads as "didn't run".
        if row["skipped"]:
            reason = row["skip_message"] or "skipped"
            line = f"{name.ljust(w['name'])}{_COL_SEP}{reason}"
            self._term.print(sgr(line, "dim", enabled=self._term.color))
            return

        cells = [name.ljust(w["name"])]
        if self._show_variant:
            variant = row.get("variant") or "-"
            if len(variant) > w["variant"]:
                variant = variant[: w["variant"] - 1] + "…"
            cells.append(variant.ljust(w["variant"]))
        if self._show_label:
            if len(label) > w["label"]:
                label = label[: w["label"] - 1] + "…"
            cells.append(label.ljust(w["label"]))
        cells += [
            f"{row['iterations']:,}".rjust(w["iters"]),
            f"{row['real_time']:.2f} {unit}".rjust(w["real"]),
            f"{row['cpu_time']:.2f} {unit}".rjust(w["cpu"]),
        ]
        if self._show_memory:
            mem = row.get("memory")
            cells.append((_fmt_bytes(mem["peak_bytes"]) if mem else "-").rjust(w["peak"]))
            cells.append((_fmt_bytes(mem["total_bytes"]) if mem else "-").rjust(w["alloc"]))
        if self._show_cpu:
            cpu = row.get("cpu_profile")
            cells.append((f"{cpu['sample_count']:,}" if cpu else "-").rjust(w["samples"]))
            top = cpu["top_function"] if cpu else "-"
            if len(top) > w["hottest_frame"]:
                top = top[: w["hottest_frame"] - 1] + "…"
            cells.append(top.ljust(w["hottest_frame"]))
        self._term.print(_COL_SEP.join(cells))


class ParquetReporter:
    """Write a Parquet file with one row per benchmark Run.

    Static schema; user context goes in a JSON string column ``custom`` (query via
    ``json_extract`` in DuckDB).

    Parameters
    ----------
    output : Path
        Destination file, overwritten if it exists.
    append : bool, default False
        If the file exists, concatenate this run's rows onto it (one more
        session) instead of overwriting. Per-row ``session_id`` columns keep
        sessions distinct.

    Raises
    ------
    RuntimeError
        From :meth:`finalize` when ``pyarrow`` is not installed.
    """

    def __init__(self, *, output: Path, append: bool = False) -> None:
        if sys.platform == "win32" and find_spec("tzdata") is None:
            raise RuntimeError(
                "ParquetReporter on Windows requires the `tzdata` package "
                "for pyarrow's UTC timestamp support. Install it with "
                "`pip install tzdata`."
            )
        self._output = Path(output)
        self._append = append
        self._context: dict[str, Any] = {}
        self._runs: list[RunRow] = []

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        self._runs.extend(runs)

    def finalize(self) -> None:
        if find_spec("pyarrow") is None:
            raise RuntimeError(
                "ParquetReporter requires pyarrow. Install it with "
                "`pip install pyarrow` (or `uv add pyarrow`)."
            )
        import pyarrow as pa
        import pyarrow.parquet as pq

        date = datetime.now(UTC)
        rows = [self._row(r, date) for r in self._runs]
        table = pa.Table.from_pylist(rows, schema=_parquet_schema())
        if self._append and self._output.exists():
            # promote_options fills columns absent from an older file with nulls,
            # so appending across a schema bump (e.g. pre-session files) works.
            existing = pq.read_table(self._output)
            table = pa.concat_tables([existing, table], promote_options="default")
        pq.write_table(table, str(self._output))

    def _row(self, row: RunRow, date: datetime) -> dict[str, Any]:
        ctx = self._context
        mem = row.get("memory")
        cpu = row.get("cpu_profile")
        # A row's own custom (set per variant) wins over the shared context block.
        run_custom = row.get("custom")
        custom = run_custom if run_custom is not None else ctx.get("custom")
        custom_json = json.dumps(custom, default=str) if custom else None
        counters = row["counters"]
        return {
            "name": row["name"],
            "run_name": row["run_name"],
            "family_index": row["family_index"],
            "per_family_instance_index": row["per_family_instance_index"],
            "run_type": row["run_type"],
            "aggregate_name": row["aggregate_name"],
            "repetitions": row["repetitions"],
            "repetition_index": row["repetition_index"],
            "threads": row["threads"],
            "iterations": row["iterations"],
            "real_time": row["real_time"],
            "cpu_time": row["cpu_time"],
            "real_accumulated_time": row["real_accumulated_time"],
            "cpu_accumulated_time": row["cpu_accumulated_time"],
            "time_unit": row["time_unit"],
            "label": row["label"],
            "skipped": row["skipped"],
            "skip_message": row["skip_message"],
            # pa rejects `{}` for `map_` columns; a list of (k, v) pairs handles
            # the empty case cleanly.
            "counters": list(counters.items()) if counters else [],
            "variant": row.get("variant"),
            "date": date,
            "session_id": ctx.get("session_id"),
            "session_tag": ctx.get("session_tag"),
            "host_name": ctx.get("host_name"),
            "executable": ctx.get("executable"),
            "num_cpus": ctx.get("num_cpus"),
            "mhz_per_cpu": ctx.get("mhz_per_cpu"),
            "cpu_scaling_enabled": ctx.get("cpu_scaling") == "enabled",
            "library_build_type": ctx.get("library_build_type"),
            "custom": custom_json,
            "memory": json.dumps(mem) if mem is not None else None,
            "cpu_profile": json.dumps(cpu) if cpu is not None else None,
        }


def _parquet_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("name", pa.string()),
            ("run_name", pa.string()),
            ("family_index", pa.int64()),
            ("per_family_instance_index", pa.int64()),
            ("run_type", pa.string()),
            ("aggregate_name", pa.string()),
            ("repetitions", pa.int64()),
            ("repetition_index", pa.int64()),
            ("threads", pa.int64()),
            ("iterations", pa.int64()),
            ("real_time", pa.float64()),
            ("cpu_time", pa.float64()),
            ("real_accumulated_time", pa.float64()),
            ("cpu_accumulated_time", pa.float64()),
            ("time_unit", pa.string()),
            ("label", pa.string()),
            ("skipped", pa.bool_()),
            ("skip_message", pa.string()),
            ("counters", pa.map_(pa.string(), pa.float64())),
            ("variant", pa.string()),
            ("date", pa.timestamp("us", tz="UTC")),
            ("session_id", pa.string()),
            ("session_tag", pa.string()),
            ("host_name", pa.string()),
            ("executable", pa.string()),
            ("num_cpus", pa.int64()),
            ("mhz_per_cpu", pa.float64()),
            ("cpu_scaling_enabled", pa.bool_()),
            ("library_build_type", pa.string()),
            ("custom", pa.string()),
            ("memory", pa.string()),
            ("cpu_profile", pa.string()),
        ]
    )


class Fanout:
    """Broadcast reporter callbacks to a list of underlying reporters.

    Used by :func:`mew.run` to multiplex multiple reporters. ``report_context``
    returns ``all(...)`` of the children's responses, so the strictest sub-reporter wins.

    Parameters
    ----------
    reporters : list[Reporter]
        Underlying reporters, called in iteration order.
    """

    def __init__(self, reporters: list[Reporter]) -> None:
        self._reporters = list(reporters)

    def report_context(self, context: dict[str, Any]) -> bool:
        results = [r.report_context(context) for r in self._reporters]
        return all(results)

    def report_runs(self, runs: list[RunRow]) -> None:
        for r in self._reporters:
            r.report_runs(runs)

    def finalize(self) -> None:
        for r in self._reporters:
            fn = getattr(r, "finalize", None)
            if callable(fn):
                fn()


__all__ = [
    "Fanout",
    "JSONLReporter",
    "JSONReporter",
    "ParquetReporter",
    "Reporter",
    "RichReporter",
]
