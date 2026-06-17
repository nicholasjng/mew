"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import contextlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO, cast, runtime_checkable

from mew._console import Terminal, _truncate_left, _truncate_right, sgr
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


# Closing `]}` of the streamed doc, written once at finalize (GB-style).
_JSON_CLOSER = "\n  ]\n}\n"


def _indent_block(text: str, spaces: int) -> str:
    """Re-indent every line after the first by ``spaces`` (json.dumps indents from col 0)."""
    return text.replace("\n", "\n" + " " * spaces)


def _open_sink(output: Path | TextIO | None, mode: str = "w") -> tuple[TextIO, bool]:
    """Resolve ``output`` to ``(file, owns_it)``.

    A Path is opened (owned), ``None`` → stdout, a stream is used as-is. ``mode``
    applies only to a Path sink. Appending to a ``.gz`` Path writes a new gzip
    member rather than recompressing, keeping archive appends O(new data).
    """
    if isinstance(output, Path):
        if output.name.endswith(".gz"):
            import gzip

            return cast(TextIO, gzip.open(output, mode + "t")), True
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

    Streams forward-only, exactly like Google Benchmark's own JSON reporter:
    :meth:`report_context` writes the context and the opening bracket, each
    :meth:`report_runs` appends rows, and :meth:`finalize` writes the closing
    ``]}``. Never seeks, so files, pipes, and stdout behave identically. The
    document is valid JSON only after finalize; for an interruption-safe
    archive use :class:`JSONLReporter`.

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
        self._first_row = True

    def report_context(self, context: dict[str, Any]) -> bool:
        self._fh, self._owns_fh = _open_sink(self._output)
        # default=str: don't crash on Path/datetime; lossy by design.
        ctx = _indent_block(json.dumps(_build_context(context), indent=2, default=str), 2)
        self._fh.write('{\n  "context": ' + ctx + ',\n  "benchmarks": [')
        self._fh.flush()
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for row in runs:
            prefix = "" if self._first_row else ","
            self._first_row = False
            rendered = _indent_block(json.dumps(row, indent=2, default=str), 4)
            self._fh.write(f"{prefix}\n    {rendered}")
        self._fh.flush()

    def finalize(self) -> None:
        if self._fh is not None:
            self._fh.write(_JSON_CLOSER)
            self._fh.flush()
        _close_sink(self._fh, self._owns_fh)


# Context fields stamped onto every JSONL row so each line is self-contained.
# Optional identity (session_id/session_tag/custom) is stamped only when present.
_ROW_STAMP_FIELDS = ("date", "host_name", "num_cpus", "cpu_scaling_enabled")
_ROW_STAMP_OPTIONAL = ("session_id", "session_tag", "custom")


class JSONLReporter:
    """Stream one self-contained JSON object per Run, one per line, flushed as runs land.

    Append-only, so it works on pipes and survives interruption. Every row carries
    its own session identity, making the file plain NDJSON (see
    docs/guide/reporters.md for querying it).

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        Destination. A Path is opened and closed here (gzip for ``.gz``); a stream
        is written directly; ``None`` writes to ``sys.stdout``.
    append : bool, default False
        Open a Path sink in append mode, adding this run's rows as a new session.
        Ignored for stream / stdout sinks.
    header : bool, default False
        Channel mode: write a ``{"context": {...}}`` line and leave the rows bare.
        Used by the ``--variant`` worker, whose parent stamps its own shared session
        onto the merged rows; row-stamping here would let the child's throwaway
        identity shadow the parent's.
    """

    def __init__(
        self,
        *,
        output: Path | TextIO | None = None,
        append: bool = False,
        header: bool = False,
    ) -> None:
        self._output = output
        self._append = append
        self._header = header
        self._fh: TextIO | None = None
        self._owns_fh = False
        self._stamp: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> bool:
        self._fh, self._owns_fh = _open_sink(self._output, "a" if self._append else "w")
        ctx = _build_context(context)
        if self._header:
            self._fh.write(json.dumps({"context": ctx}, default=str) + "\n")
            self._fh.flush()
        else:
            self._stamp = {k: ctx.get(k) for k in _ROW_STAMP_FIELDS}
            self._stamp.update({k: ctx[k] for k in _ROW_STAMP_OPTIONAL if k in ctx})
        return True

    def report_runs(self, runs: list[RunRow]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for row in runs:
            # Row-carried values win: merged --variant rows bring their own
            # per-variant `custom` (and `variant`), which must not be clobbered.
            self._fh.write(json.dumps({**self._stamp, **row}, default=str) + "\n")
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
        Add a ``Peak Mem`` column.
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
        name = _truncate_left(name, w["name"])

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
            variant = _truncate_right(variant, w["variant"])
            cells.append(variant.ljust(w["variant"]))
        if self._show_label:
            label = _truncate_right(label, w["label"])
            cells.append(label.ljust(w["label"]))
        cells += [
            f"{row['iterations']:,}".rjust(w["iters"]),
            f"{row['real_time']:.2f} {unit}".rjust(w["real"]),
            f"{row['cpu_time']:.2f} {unit}".rjust(w["cpu"]),
        ]
        if self._show_memory:
            mem = row.get("memory")
            cells.append((_fmt_bytes(mem["peak_bytes"]) if mem else "-").rjust(w["peak"]))
        if self._show_cpu:
            cpu = row.get("cpu_profile")
            cells.append((f"{cpu['sample_count']:,}" if cpu else "-").rjust(w["samples"]))
            top = cpu["top_function"] if cpu else "-"
            top = _truncate_right(top, w["hottest_frame"])
            cells.append(top.ljust(w["hottest_frame"]))
        self._term.print(_COL_SEP.join(cells))


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
        # ExitStack unwinds LIFO, so register in reverse to finalize in call order.
        with contextlib.ExitStack() as stack:
            for r in reversed(self._reporters):
                fn = getattr(r, "finalize", None)
                if callable(fn):
                    stack.callback(fn)


__all__ = [
    "Fanout",
    "JSONLReporter",
    "JSONReporter",
    "Reporter",
    "RichReporter",
]
