"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Protocol, TextIO, cast, runtime_checkable

from mew._console import _COL_SEP, Terminal, _truncate_left, _truncate_right, sgr
from mew._typing import BenchmarkResult


@runtime_checkable
class Reporter(Protocol):
    """Duck-typed reporter interface consumed by the C++ runner.

    ``report_context`` and ``report_runs`` are required; ``finalize`` is optional.
    All callbacks run on the main thread with the GIL held. Raise to stop the run:
    the exception aborts the remaining benchmarks and propagates out of :func:`mew.run`.

    Methods
    -------
    report_context(context)
        Called once before any runs with the C++ context dict.
    report_runs(runs)
        Called one or more times with a list of :class:`~mew._typing.BenchmarkResult`
        dicts (runs projected from the C++ ``Run``).
    """

    def report_context(self, context: dict[str, Any], /) -> None: ...
    def report_runs(self, runs: list[BenchmarkResult], /) -> None: ...


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
# `/case:N` at the end of a name, or before Google Benchmark's aggregate suffix
# (`_mean`, `_median`, ...), which it appends *after* the args part.
_CASE_SUFFIX_RE = re.compile(r"/case:\d+(?=$|_)")


def strip_reserved_suffixes(name: str) -> str:
    """Drop the trailing Google Benchmark option/case suffixes from ``name``.

    The half of :func:`canonical_name` that does not need a label, so
    :mod:`mew.api` can reject a registered name that would be silently regrouped
    when results are read back, without restating the grammar.
    """
    return _CASE_SUFFIX_RE.sub("", _OPTION_SUFFIXES_RE.sub("", name))


def canonical_name(name: str, label: Any) -> str:
    """Strip GB option suffixes and render a parametrize case by its human label.

    ``bench.py::f/case:0/min_time:0.200`` with label ``n=10000`` becomes
    ``bench.py::f[n=10000]``. Shared by :class:`RichReporter` and :mod:`mew.compare`
    so both show the same name; the stored ``name`` field stays the raw GB name
    (compare canonicalizes on read).

    An aggregate row's ``_mean``/``_median``/… suffix is preserved, so it stays
    distinguishable from the per-repetition rows it summarizes:
    ``bench.py::f/case:0_mean`` becomes ``bench.py::f[n=10000]_mean``.
    """
    name = _OPTION_SUFFIXES_RE.sub("", name)
    if label and isinstance(label, str) and (m := _CASE_SUFFIX_RE.search(name)):
        # Rebuild rather than substitute: the label bracket replaces the case
        # index in place, keeping any aggregate suffix trailing it.
        return f"{name[: m.start()]}[{label}]{name[m.end() :]}"
    return name


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

    def report_context(self, context: dict[str, Any]) -> None:
        self._fh, self._owns_fh = _open_sink(self._output)
        # A reporter instance may be reused for several mew.run() calls. Each
        # context starts a fresh JSON document, so its first row must not inherit
        # the comma state from the preceding document.
        self._first_row = True
        # default=str: don't crash on Path/datetime; lossy by design.
        ctx = _indent_block(json.dumps(context, indent=2, default=str), 2)
        self._fh.write('{\n  "context": ' + ctx + ',\n  "benchmarks": [')
        self._fh.flush()

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
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
        self._fh = None
        self._owns_fh = False


# Stamped onto every JSONL row so each line stands alone: `session` is what
# compare groups and orders by, `context` is the provenance that goes with it.
# Two keys, so a new context field never widens the row schema.
_ROW_STAMP_FIELDS = ("session", "context")


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
    """

    def __init__(
        self,
        *,
        output: Path | TextIO | None = None,
        append: bool = False,
    ) -> None:
        self._output = output
        self._append = append
        self._fh: TextIO | None = None
        self._owns_fh = False
        self._stamp: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._fh, self._owns_fh = _open_sink(self._output, "a" if self._append else "w")
        self._stamp = {k: context[k] for k in _ROW_STAMP_FIELDS if k in context}

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for row in runs:
            # Preserve row-carried context when merging data from another process.
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
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
        show_label: bool = False,
    ) -> None:
        self._term = terminal or Terminal()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
        self._show_label = show_label
        self._context: dict[str, Any] = {}
        self._widths: dict[str, int] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._context = dict(context)
        self._print_banner()
        self._compute_widths()
        self._print_header()

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        for row in runs:
            self._print_row(row)

    def finalize(self) -> None:
        pass

    def _print_banner(self) -> None:
        c = self._context
        host = c.get("host_name") or "?"
        cpus = c.get("num_cpus", "?")
        scaling = c.get("cpu_scaling", "?")
        color = self._term.color

        def cy(v: object) -> str:
            return sgr(str(v), "cyan", enabled=color)

        self._term.print(
            f"{sgr('mew', 'bold', enabled=color)} {sgr('·', 'dim', enabled=color)} "
            f"host={cy(host)} cpus={cy(cpus)} scaling={cy(scaling)}"
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

    def _print_row(self, row: BenchmarkResult) -> None:
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
            # Numeric profile values cross the binding as doubles; a sample
            # count is conceptually an integer, so render it as one.
            cells.append((f"{int(cpu['sample_count']):,}" if cpu else "-").rjust(w["samples"]))
            top = cpu["top_function"] if cpu else "-"
            top = _truncate_right(top, w["hottest_frame"])
            cells.append(top.ljust(w["hottest_frame"]))
        self._term.print(_COL_SEP.join(cells))


class Fanout:
    """Broadcast reporter callbacks to a list of underlying reporters.

    Used by :func:`mew.run` to multiplex multiple reporters. A child that raises
    stops the run, so the strictest sub-reporter wins.

    Parameters
    ----------
    reporters : list[Reporter]
        Underlying reporters, called in iteration order.
    """

    def __init__(self, reporters: list[Reporter]) -> None:
        self._reporters = list(reporters)

    def report_context(self, context: dict[str, Any]) -> None:
        for r in self._reporters:
            r.report_context(context)

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
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
