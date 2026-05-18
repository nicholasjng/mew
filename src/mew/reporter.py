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

    Implementations only need ``report_context`` and ``report_runs``;
    ``finalize`` is optional. All callbacks run on the main thread with the
    GIL held.

    Methods
    -------
    report_context(context)
        Called once before any runs with the C++ context dict
        (``host_name``, ``num_cpus``, …). Returning ``False`` aborts the
        Google Benchmark run.
    report_runs(runs)
        Called one or more times with completed
        :class:`~mew._core.Run` objects (possibly wrapped as
        :class:`~mew._profile.EnrichedRun`).
    """

    def report_context(self, context: dict[str, Any]) -> bool: ...
    def report_runs(self, runs: list[Run]) -> None: ...


_COL_SEP = " │ "


def _fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def _run_to_dict(r: Run | EnrichedRun) -> dict[str, Any]:
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
        "counters": dict(r.counters) if r.counters else {},
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


class JSONReporter:
    """Emit a single JSON document modeled on Google Benchmark's own format.

    Buffers context and runs until :meth:`finalize` is called, then serializes
    a ``{"context": ..., "benchmarks": [...]}`` document.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A :class:`~pathlib.Path` is written via
        :meth:`Path.write_text`; a text stream is written to directly; ``None``
        writes to ``sys.stdout``.
    """

    def __init__(self, *, output: Path | TextIO | None = None) -> None:
        self._output = output
        self._context: dict[str, Any] = {}
        self._runs: list[Run] = []

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        return True

    def report_runs(self, runs: list[Run]) -> None:
        self._runs.extend(runs)

    def finalize(self) -> None:
        ctx: dict[str, Any] = {
            "date": datetime.now(UTC).isoformat(),
            "host_name": self._context.get("host_name"),
            "executable": self._context.get("executable"),
            "num_cpus": self._context.get("num_cpus"),
            "mhz_per_cpu": self._context.get("mhz_per_cpu"),
            "cpu_scaling_enabled": self._context.get("cpu_scaling") == "enabled",
            "library_build_type": self._context.get("library_build_type"),
        }
        custom = self._context.get("custom")
        if custom:
            ctx["custom"] = custom

        doc = {
            "context": ctx,
            "benchmarks": [_run_to_dict(r) for r in self._runs],
        }
        # `default=str` keeps non-JSON-native values (Path, datetime, etc.) from
        # crashing the serializer. Lossy by design — encourage users to put
        # JSON-friendly values in context.
        text = json.dumps(doc, indent=2, default=str)
        if isinstance(self._output, Path):
            self._output.write_text(text + "\n")
        elif self._output is None:
            sys.stdout.write(text + "\n")
        else:
            self._output.write(text + "\n")


class RichReporter:
    """Stream one row per benchmark family as Google Benchmark completes it.

    The header is printed before any results land, so the optional-column
    flags must be passed up front rather than auto-detected from the runs.

    Parameters
    ----------
    console : rich.console.Console, optional
        Rich console to print to. Defaults to a fresh
        :class:`~rich.console.Console`.
    show_memory : bool, default False
        Add ``Peak Mem`` / ``Total Alloc`` columns and read per-run memory
        profiles via :class:`~mew._profile.EnrichedRun`.
    show_cpu : bool, default False
        Add ``Samples`` / ``Hottest Frame`` columns and read per-run CPU
        profiles. The CLI wires these flags from
        ``--profile-memory`` / ``--profile-cpu``.
    """

    def __init__(
        self,
        *,
        console: Console | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
    ) -> None:
        self._console = console or Console()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
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
        cells = [
            "Benchmark".ljust(w["name"]),
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

    def _print_row(self, r: Any) -> None:
        w = self._widths
        unit = r.time_unit.name
        name = r.benchmark_name()
        if len(name) > w["name"]:
            name = name[: w["name"] - 1] + "…"

        cells = [
            name.ljust(w["name"]),
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

    The schema is static; arbitrarily-shaped user context is encoded as a
    JSON string column named ``custom``. Query it from DuckDB with
    ``json_extract``::

        SELECT name, real_time,
               counters['rss_kb'] AS rss_kb,
               json_extract(custom, '$.dataset.size') AS dataset_size
        FROM 'results.parquet';

    Parameters
    ----------
    output : Path
        Destination Parquet file. Overwritten if it exists.

    Raises
    ------
    RuntimeError
        From :meth:`finalize` when ``pyarrow`` is not installed.
    """

    def __init__(self, *, output: Path) -> None:
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
            # pyarrow.from_pylist accepts a list of (key, value) pairs for
            # `map_` columns. dict would also work for non-empty maps but
            # pa rejects {} so a list keeps the empty case clean.
            "counters": list(r.counters.items()) if r.counters else [],
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
    ``report_context`` returns ``all(...)`` of the children's responses —
    Google Benchmark halts when a reporter returns ``False``, so the
    strictest sub-reporter wins.

    Parameters
    ----------
    reporters : list[Reporter]
        Underlying reporters. Calls are dispatched in iteration order.
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
    "JSONReporter",
    "ParquetReporter",
    "Reporter",
    "RichReporter",
]
