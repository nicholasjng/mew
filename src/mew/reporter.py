"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, runtime_checkable

from rich.console import Console
from rich.table import Table

from mew._core import Run


@runtime_checkable
class Reporter(Protocol):
    """Duck-typed reporter interface consumed by the C++ runner.

    Implementations only need `report_context` and `report_runs`; `finalize`
    is optional. All callbacks run on the main thread with the GIL held.
    """

    def report_context(self, context: dict[str, Any]) -> bool: ...
    def report_runs(self, runs: list[Run]) -> None: ...


def _run_to_dict(r: Run) -> dict[str, Any]:
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
        "counters": dict(r.counters) if r.counters else {},
    }


class JSONReporter:
    """Emit a single JSON document modeled on Google Benchmark's own format."""

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
    """Print a colorized summary table at the end of the run."""

    def __init__(self, *, console: Console | None = None) -> None:
        self._console = console or Console()
        self._context: dict[str, Any] = {}
        self._runs: list[Run] = []

    def report_context(self, context: dict[str, Any]) -> bool:
        self._context = dict(context)
        return True

    def report_runs(self, runs: list[Run]) -> None:
        self._runs.extend(runs)

    def finalize(self) -> None:
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

        t = Table(show_header=True, header_style="bold")
        t.add_column("Benchmark", overflow="fold")
        t.add_column("Iters", justify="right")
        t.add_column("Real", justify="right")
        t.add_column("CPU", justify="right")
        t.add_column("Label", overflow="fold")
        for r in self._runs:
            unit = r.time_unit.name
            t.add_row(
                r.benchmark_name(),
                f"{r.iterations:,}",
                f"{r.adjusted_real_time():.2f} {unit}",
                f"{r.adjusted_cpu_time():.2f} {unit}",
                r.report_label or "",
            )
        self._console.print(t)


class ParquetReporter:
    """Write a Parquet file with one row per benchmark Run.

    Requires `pyarrow` (not installed by default — `pip install pyarrow`). The
    schema is static; arbitrarily-shaped user context is encoded as a JSON
    string column named `custom`. Query it from DuckDB with `json_extract`::

        SELECT name, real_time,
               counters['rss_kb'] AS rss_kb,
               json_extract(custom, '$.dataset.size') AS dataset_size
        FROM 'results.parquet';
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
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - exercised when pyarrow absent
            raise RuntimeError(
                "ParquetReporter requires pyarrow. Install it with "
                "`pip install pyarrow` (or `uv add pyarrow`)."
            ) from exc

        date = datetime.now(UTC)
        ctx = self._context
        custom = ctx.get("custom")
        custom_json = json.dumps(custom, default=str) if custom else None

        rows = [self._row(r, date, custom_json) for r in self._runs]
        table = pa.Table.from_pylist(rows, schema=_parquet_schema(pa))
        pq.write_table(table, str(self._output))

    def _row(self, r: Run, date: datetime, custom_json: str | None) -> dict[str, Any]:
        ctx = self._context
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
        }


def _parquet_schema(pa: Any) -> Any:
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
        ]
    )


class Fanout:
    """Broadcast reporter callbacks to a list of underlying reporters."""

    def __init__(self, reporters: list[Reporter]) -> None:
        self._reporters = list(reporters)

    def report_context(self, context: dict[str, Any]) -> bool:
        # If any sub-reporter rejects the context, GB will halt the run. We
        # AND the responses so the strictest one wins.
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
