"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TextIO, runtime_checkable

from rich.console import Console
from rich.text import Text

from mew._core import Run
from mew._profile import EnrichedRun

if TYPE_CHECKING:
    from mew.cpu import CPUProfile
    from mew.memory import MemoryProfile


@runtime_checkable
class Reporter(Protocol):
    """Duck-typed reporter interface consumed by the C++ runner.

    Implementations must provide ``report_context`` and ``report_runs``; ``finalize`` is optional.
    All callbacks run on the main thread with the GIL held.

    Methods
    -------
    report_context(context)
        Called once before any runs with the C++ context dict.
        Returning ``False`` aborts the Google Benchmark run.
    report_runs(runs)
        Called one or more times with completed :class:`~mew._core.Run` objects.
    """

    def report_context(self, context: dict[str, Any]) -> bool: ...
    def report_runs(self, runs: list[Run]) -> None: ...


_COL_SEP = " │ "


def _fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def _build_context(context: dict[str, Any]) -> dict[str, Any]:
    """Project the raw C++ context dict into the serialized ``context`` block.

    Shared by :class:`JSONReporter` and :class:`JSONLReporter` so both emit the
    same shape. ``date`` is stamped at call time.
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
    custom = context.get("custom")
    if custom:
        ctx["custom"] = custom
    return ctx


def _run_to_dict(r: Run | EnrichedRun) -> dict[str, Any]:
    counters = r.counters  # hot path on C++ Run: each access rebuilds the dict
    d: dict[str, Any] = {
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
    mem: MemoryProfile | None = getattr(r, "memory", None)
    if mem is not None:
        d["memory"] = {
            "profiler": mem.profiler,
            "peak_bytes": mem.peak_bytes,
            "total_bytes": mem.total_bytes,
            "total_allocations": mem.total_allocations,
        }
    cpu: CPUProfile | None = getattr(r, "cpu", None)
    if cpu is not None:
        d["cpu_profile"] = {
            "profiler": cpu.profiler,
            "wall_time": cpu.wall_time,
            "sample_count": cpu.sample_count,
            "top_function": cpu.top_function,
            "top_function_total_self_time": cpu.top_function_total_self_time,
        }
    return d


# Closing `]}` of the streamed doc, rewritten over itself each flush so the file
# stays valid JSON.
_JSON_CLOSER = "\n  ]\n}\n"


def _indent_block(text: str, spaces: int) -> str:
    """Re-indent every line after the first by ``spaces`` (json.dumps indents from col 0)."""
    return text.replace("\n", "\n" + " " * spaces)


def _open_sink(output: Path | TextIO | None) -> tuple[TextIO, bool]:
    """Resolve ``output`` to ``(file, owns_it)``: a Path is opened (owned), ``None`` → stdout, a stream is used as-is."""
    if isinstance(output, Path):
        return output.open("w"), True
    if output is None:
        return sys.stdout, False
    return output, False


def _close_sink(fh: TextIO | None, owns_fh: bool) -> None:
    """Close ``fh`` only if this reporter opened it."""
    if owns_fh and fh is not None:
        fh.close()


class JSONReporter:
    """Emit a single ``{"context": ..., "benchmarks": [...]}`` document, GB-style.

    To a **seekable** sink (a file) it streams: writes context + empty array up
    front, then each :meth:`report_runs` seeks over the closing ``]}`` and re-writes
    it, so the file is valid JSON after every flush (survives Ctrl-C). A non-seekable
    sink (stdout, pipe) can't be rewritten, so it buffers and writes once at :meth:`finalize`.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A Path is opened and closed here; a stream is written directly;
        ``None`` writes to ``sys.stdout``.
    """

    def __init__(self, *, output: Path | TextIO | None = None) -> None:
        self._output = output
        self._context: dict[str, Any] = {}
        self._runs: list[Run] = []  # buffered (non-seekable) path only
        self._fh: TextIO | None = None
        self._owns_fh = False
        self._streaming = False
        # Byte offset of the closer, i.e. where the next row gets written.
        self._reopen_pos = 0
        self._first_row = True

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        self._fh, self._owns_fh = _open_sink(self._output)
        self._streaming = self._fh.seekable()
        if self._streaming:
            # default=str: don't crash on Path/datetime; lossy by design.
            ctx = _indent_block(json.dumps(_build_context(context), indent=2, default=str), 2)
            self._fh.write('{\n  "context": ' + ctx + ',\n  "benchmarks": [')
            self._reopen_pos = self._fh.tell()
            self._fh.write(_JSON_CLOSER)
            self._fh.flush()
        return True

    def report_runs(self, runs: list[Run]) -> None:
        if not self._streaming:
            self._runs.extend(runs)
            return
        assert self._fh is not None  # report_context runs first, always
        # Overwrite the closer with the rows, then re-close. Rows are >> the closer,
        # so no stale bytes; empty `runs` just rewrites it in place.
        self._fh.seek(self._reopen_pos)
        for r in runs:
            prefix = "" if self._first_row else ","
            self._first_row = False
            row = _indent_block(json.dumps(_run_to_dict(r), indent=2, default=str), 4)
            self._fh.write(f"{prefix}\n    {row}")
        self._reopen_pos = self._fh.tell()
        self._fh.write(_JSON_CLOSER)
        self._fh.flush()

    def finalize(self) -> None:
        if not self._streaming:
            # Non-seekable sink: emit the whole document in one shot.
            assert self._fh is not None
            doc = {
                "context": _build_context(self._context),
                "benchmarks": [_run_to_dict(r) for r in self._runs],
            }
            self._fh.write(json.dumps(doc, indent=2, default=str) + "\n")
        _close_sink(self._fh, self._owns_fh)


class JSONLReporter:
    """Stream one JSON object per Run, one per line, flushed as runs land.

    Append-only (works on pipes), so a long suite leaves a growing, ``tail``-able
    file that survives interruption. Line 1 is a ``{"context": {...}}`` header; each
    later line is one benchmark record. Consumers skip line 1 / branch on ``context``.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A Path is opened and closed here; a stream is written directly;
        ``None`` writes to ``sys.stdout``.
    """

    def __init__(self, *, output: Path | TextIO | None = None) -> None:
        self._output = output
        self._fh: TextIO | None = None
        self._owns_fh = False

    def report_context(self, context: dict[str, Any]) -> bool:
        self._fh, self._owns_fh = _open_sink(self._output)
        # Header first so provenance lands before any rows.
        self._fh.write(json.dumps({"context": _build_context(context)}, default=str) + "\n")
        self._fh.flush()
        return True

    def report_runs(self, runs: list[Run]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for r in runs:
            self._fh.write(json.dumps(_run_to_dict(r), default=str) + "\n")
        self._fh.flush()

    def finalize(self) -> None:
        _close_sink(self._fh, self._owns_fh)


class RichReporter:
    """Stream one row per benchmark to a terminal as runs complete.

    The header prints before any results land, so optional-column flags are passed up front.

    Parameters
    ----------
    console : rich.console.Console, optional
        Console to print to. Defaults to a fresh one.
    show_memory : bool, default False
        Add ``Peak Mem`` / ``Total Alloc`` columns.
    show_cpu : bool, default False
        Add ``Samples`` / ``Hottest Frame`` columns.
    show_label : bool, default False
        Add a ``Label`` column (the parametrize case id). Pass for families, where
        the case is otherwise indistinguishable from the truncated name.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
        show_label: bool = False,
    ) -> None:
        self._console = console or Console()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
        self._show_label = show_label
        self._context: dict[str, Any] = {}
        self._widths: dict[str, int] = {}

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        self._print_banner()
        self._compute_widths()
        self._print_header()
        return True

    def report_runs(self, runs: list[Run]) -> None:
        for r in runs:
            self._print_row(r)

    def finalize(self) -> None:
        pass

    def _print_banner(self) -> None:
        c = self._context
        host = c.get("host_name") or "?"
        cpus = c.get("num_cpus", "?")
        mhz = c.get("mhz_per_cpu", 0) or 0
        scaling = c.get("cpu_scaling", "?")
        self._console.print(
            f"[bold]mew[/] [dim]·[/] host=[cyan]{host}[/] "
            f"cpus=[cyan]{cpus}[/] @ [cyan]{mhz:.0f}MHz[/] "
            f"scaling=[cyan]{scaling}[/]"
        )

    def _compute_widths(self) -> None:
        fixed: dict[str, int] = {
            "iters": 12,
            "real": 14,
            "cpu": 14,
        }
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
        name_w = max(30, self._console.width - sum(fixed.values()) - spacing)
        self._widths = {"name": name_w, **fixed}

    def _print_header(self) -> None:
        w = self._widths
        cells = ["Benchmark".ljust(w["name"])]
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
        self._console.print(Text(line, style="bold"))
        self._console.print(Text("─" * len(line), style="dim"))

    def _print_row(self, r: Run | EnrichedRun) -> None:
        w = self._widths
        unit = r.time_unit.name
        name = r.benchmark_name()
        # Left-ellipsize: keep the disambiguating function suffix / case:N tail.
        if len(name) > w["name"]:
            name = "…" + name[-(w["name"] - 1) :]

        cells = [name.ljust(w["name"])]
        if self._show_label:
            label = r.report_label
            if len(label) > w["label"]:
                label = label[: w["label"] - 1] + "…"
            cells.append(label.ljust(w["label"]))
        cells += [
            f"{r.iterations:,}".rjust(w["iters"]),
            f"{r.adjusted_real_time():.2f} {unit}".rjust(w["real"]),
            f"{r.adjusted_cpu_time():.2f} {unit}".rjust(w["cpu"]),
        ]
        if self._show_memory:
            mem: MemoryProfile | None = getattr(r, "memory", None)
            cells.append((_fmt_bytes(mem.peak_bytes) if mem else "-").rjust(w["peak"]))
            cells.append((_fmt_bytes(mem.total_bytes) if mem else "-").rjust(w["alloc"]))
        if self._show_cpu:
            cpu: CPUProfile | None = getattr(r, "cpu", None)
            cells.append((f"{cpu.sample_count:,}" if cpu else "-").rjust(w["samples"]))
            top = cpu.top_function if cpu else "-"
            if len(top) > w["hottest_frame"]:
                top = top[: w["hottest_frame"] - 1] + "…"
            cells.append(top.ljust(w["hottest_frame"]))
        self._console.print(Text(_COL_SEP.join(cells)))


class ParquetReporter:
    """Write a Parquet file with one row per benchmark Run.

    Static schema; user context goes in a JSON string column ``custom`` (query via
    ``json_extract`` in DuckDB).

    Parameters
    ----------
    output : Path
        Destination file, overwritten if it exists.

    Raises
    ------
    RuntimeError
        From :meth:`finalize` when ``pyarrow`` is not installed.
    """

    def __init__(self, *, output: Path) -> None:
        if sys.platform == "win32" and find_spec("tzdata") is None:
            raise RuntimeError(
                "ParquetReporter on Windows requires the `tzdata` package "
                "for pyarrow's UTC timestamp support. Install it with "
                "`pip install tzdata`."
            )
        self._output = Path(output)
        self._context: dict[str, Any] = {}
        self._runs: list[Run] = []

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        return True

    def report_runs(self, runs: list[Run]) -> None:
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
        ctx = self._context
        custom = ctx.get("custom")
        custom_json = json.dumps(custom, default=str) if custom else None

        rows = [self._row(r, date, custom_json) for r in self._runs]
        table = pa.Table.from_pylist(rows, schema=_parquet_schema())
        pq.write_table(table, str(self._output))

    def _row(self, r: Run | EnrichedRun, date: datetime, custom_json: str | None) -> dict[str, Any]:
        ctx = self._context
        mem: MemoryProfile | None = getattr(r, "memory", None)
        cpu: CPUProfile | None = getattr(r, "cpu", None)
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
            # pa rejects `{}` for `map_` columns; a list of (k, v) pairs handles
            # the empty case cleanly.
            "counters": list(counters.items()) if counters else [],
            "date": date,
            "host_name": ctx.get("host_name"),
            "executable": ctx.get("executable"),
            "num_cpus": ctx.get("num_cpus"),
            "mhz_per_cpu": ctx.get("mhz_per_cpu"),
            "cpu_scaling_enabled": ctx.get("cpu_scaling") == "enabled",
            "library_build_type": ctx.get("library_build_type"),
            "custom": custom_json,
            "memory": json.dumps(
                {
                    "profiler": mem.profiler,
                    "peak_bytes": mem.peak_bytes,
                    "total_bytes": mem.total_bytes,
                    "total_allocations": mem.total_allocations,
                }
            )
            if mem is not None
            else None,
            "cpu_profile": json.dumps(
                {
                    "profiler": cpu.profiler,
                    "wall_time": cpu.wall_time,
                    "sample_count": cpu.sample_count,
                    "top_function": cpu.top_function,
                    "top_function_total_self_time": cpu.top_function_total_self_time,
                }
            )
            if cpu is not None
            else None,
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
            ("date", pa.timestamp("us", tz="UTC")),
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

    Used by :func:`mew.run` to multiplex when multiple reporters are passed.
    ``report_context`` returns ``all(...)`` of the children's responses, so the strictest sub-reporter wins.

    Parameters
    ----------
    reporters : list[Reporter]
        Underlying reporters.
        Calls are dispatched in iteration order.
    """

    def __init__(self, reporters: list[Reporter]) -> None:
        self._reporters = list(reporters)

    def report_context(self, context: dict[str, Any]) -> bool:
        results = [r.report_context(context) for r in self._reporters]
        return all(results) if results else True

    def report_runs(self, runs: list[Run]) -> None:
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
