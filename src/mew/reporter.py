"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import contextlib
import json
import statistics
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TextIO, cast, runtime_checkable

from mew._console import _COL_SEP, Table, Terminal, _truncate_left, fill, sgr
from mew._typing import BenchmarkResult


@runtime_checkable
class Reporter(Protocol):
    """What :func:`mew.run` hands results to.

    All callbacks run on the main thread. One that raises aborts the remaining
    benchmarks, and the exception propagates out of :func:`mew.run`.

    Methods
    -------
    report_context(context)
        Called once before any results, with the run's ``session`` and
        ``context`` blocks.
    report_runs(runs)
        Called as results arrive, with a list of :class:`mew.BenchmarkResult`.
    finalize()
        Optional; called once after the last result, also when the run aborts.
    """

    def report_context(self, context: dict[str, Any], /) -> None: ...
    def report_runs(self, runs: list[BenchmarkResult], /) -> None: ...


_NS_PER_UNIT = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}


def _to_ns(value: float, unit: str | None) -> float:
    """Normalize a ``(value, unit)`` pair to nanoseconds.

    Compared files can declare different units (e.g. ``--benchmark_time_unit=us``),
    so delta/speedup math must go through this, not raw ``sample.value``.
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


def _fmt_time(value: float, unit: str | None) -> str:
    scaled, out_unit = _scale_time(value, unit)
    return f"{scaled:.2f} {out_unit}"


def _fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def _fmt_function(top: object) -> str:
    """``work (bench.py:6)`` from a :class:`~mew.cpu.TopFunction`, as far as it goes."""
    if not isinstance(top, Mapping):  # A custom profiler's summary.
        return str(top)
    where = ":".join(str(part) for part in (top["file"], top["line"]) if part is not None)
    return f"{top['function']} ({where})" if where else top["function"]


def _fmt_profile(cpu: Mapping[str, Any] | None) -> list[str]:
    """``42 samples``, ``hottest work (bench.py:6)`` and ``BINARY_OP 62%, 62% generic``."""
    if cpu is None:  # The pass ended before its loop, or the profiler had nothing to say.
        return ["no profile"]
    parts = []
    if (count := cpu.get("sample_count")) is not None:
        parts.append(f"{count:,} sample{'' if count == 1 else 's'}")
    if top := cpu.get("top_function"):
        parts.append(f"hottest {_fmt_function(top)}")
    if instructions := cpu.get("top_instructions"):
        parts.append(f"{instructions[0]['instruction']} {instructions[0]['share']:.0%}")
        if (generic := cpu.get("unspecialized_share") or 0.0) >= 0.005:
            parts[-1] += f", {generic:.0%} generic"
    return parts


_BIG_SI = ("k", "M", "G", "T", "P", "E", "Z", "Y")
_BIG_IEC = ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi", "Yi")
_SMALL_SI = ("m", "u", "n", "p", "f", "a", "z", "y")


def _human_readable(value: float, one_k: int, exponent: bool) -> str:
    """Google Benchmark's ``HumanReadableNumber`` (string_util.cc), one digit of precision."""
    sign, v, power = "-" if value < 0 else "", abs(value), 0
    if v > one_k - 1:
        scaled = v
        for i in range(len(_BIG_SI)):
            scaled /= one_k
            if scaled <= one_k - 1:
                v, power = scaled, i + 1
                break
    elif v < 0.01:  # values in [0.01, 1) print as they are
        scaled = v
        for i in range(len(_SMALL_SI)):
            scaled *= one_k
            if scaled >= 1:
                v, power = scaled, -(i + 1)
                break
    mantissa = f"{sign}{v:g}"
    if exponent:
        return f"{mantissa}*2^{power * 10}" if one_k == 1024 else f"{mantissa}e{power * 3}"
    if power > 0:
        return mantissa + (_BIG_IEC if one_k == 1024 else _BIG_SI)[power - 1]
    return mantissa + (_SMALL_SI[-power - 1] if power < 0 else "")


def _fmt_counters(row: BenchmarkResult) -> list[str]:
    """Counters as Google Benchmark's console shows them: ``name=1.2Gi/s``."""
    parts = []
    for counter in row["counters"]:
        fmt = counter.get("format", {})
        text = _human_readable(counter["value"], fmt.get("one_k", 1000), fmt.get("exponent", False))
        if fmt.get("rate"):
            text += "s" if fmt.get("inverted") else "/s"
        parts.append(f"{counter['name']}={text}")
    return parts


# Stamped onto every row so each one stands alone, in JSON and JSONL alike.
_ROW_STAMP_FIELDS = ("session", "context")

_JSON_CLOSER = "\n  ]\n}\n"


def _indent_block(text: str, spaces: int) -> str:
    """Re-indent every line after the first by ``spaces`` (json.dumps indents from col 0)."""
    return text.replace("\n", "\n" + " " * spaces)


def _open_text(path: Path, mode: str = "r") -> TextIO:
    """Open ``path`` as text, through gzip when its name ends in ``.gz`` in any case."""
    if path.name.lower().endswith(".gz"):
        import gzip

        return cast(TextIO, gzip.open(path, mode + "t"))
    return cast(TextIO, path.open(mode))


def _open_sink(output: Path | TextIO | None, mode: str = "w") -> tuple[TextIO, bool]:
    """Resolve ``output`` to a stream and an ownership flag."""
    if isinstance(output, Path):
        return _open_text(output, mode), True
    if output is None:
        return sys.stdout, False
    return output, False


def _close_sink(fh: TextIO | None, owns_fh: bool) -> None:
    """Close ``fh`` only if this reporter opened it."""
    if owns_fh and fh is not None:
        fh.close()


class JSONReporter:
    """Write one ``{"session": ..., "context": ..., "benchmarks": [...]}`` document.

    Each row carries ``session`` and ``context`` as :class:`JSONLReporter`
    writes them; the top level repeats them. The document is valid JSON only
    once the run finishes, so prefer :class:`JSONLReporter` where a run may be
    interrupted.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        A path is opened for the run and closed after it; a stream is written
        as is; ``None`` writes to ``sys.stdout``.
    """

    def __init__(self, *, output: Path | TextIO | None = None) -> None:
        self._output = output
        self._fh: TextIO | None = None
        self._owns_fh = False
        self._first_row = True
        self._stamp: dict[str, Any] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._fh, self._owns_fh = _open_sink(self._output)
        # Reset comma state when the reporter is reused.
        self._first_row = True
        self._stamp = {k: context[k] for k in _ROW_STAMP_FIELDS if k in context}
        # default=str: don't crash on Path/datetime; lossy by design.
        header = "".join(
            f'  "{key}": {_indent_block(json.dumps(value, indent=2, default=str), 2)},\n'
            for key, value in context.items()
        )
        self._fh.write("{\n" + header + '  "benchmarks": [')
        self._fh.flush()

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        assert self._fh is not None  # report_context runs first, always
        for row in runs:
            prefix = "" if self._first_row else ","
            self._first_row = False
            rendered = _indent_block(json.dumps({**self._stamp, **row}, indent=2, default=str), 4)
            self._fh.write(f"{prefix}\n    {rendered}")
        self._fh.flush()

    def finalize(self) -> None:
        if self._fh is not None:
            self._fh.write(_JSON_CLOSER)
            self._fh.flush()
        _close_sink(self._fh, self._owns_fh)
        self._fh = None
        self._owns_fh = False


class JSONLReporter:
    """Stream one JSON object per result row, a line each, flushed as rows arrive.

    Every line stands alone, so the file works on pipes and survives an
    interrupted run.

    Parameters
    ----------
    output : Path, TextIO, or None, optional
        A path is opened for the run and closed after it, gzip-compressed for
        ``.gz``; a stream is written as is; ``None`` writes to ``sys.stdout``.
    append : bool, default False
        Append to a path instead of replacing it, adding the run as a new
        session.
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
        self._fh = None
        self._owns_fh = False


class RichReporter:
    """Print mew's console table, a row per result as it arrives.

    The header prints before the first result, so the columns are chosen up
    front. Below a row, an indented line lists what has no column: a label the
    benchmark set, its counters and, with ``show_cpu``, its CPU profile. When the
    run ends, a second table sums up each repeated benchmark as ``mew compare``
    reduces it: the median times, and the stddev and cv (stddev over the mean)
    of the real time. The terminal width is read once per run.

    Parameters
    ----------
    terminal : Terminal, optional
        Where to print; defaults to stdout, colored when it is a terminal.
    show_memory : bool, default False
        Add a ``Peak Mem`` column.
    show_cpu : bool, default False
        List each row's sample count, hottest function and, where
        :func:`mew.cpu.reads_instructions`, hottest bytecode instruction.
    name_width : int, optional
        Width of the name column, such as the longest name the run will report;
        by default all the width the other columns leave.
    show_repetitions : bool, default True
        Print each repetition of a repeated benchmark. Without them, it appears
        only in the closing summary; result files keep every repetition either way.
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
        name_width: int | None = None,
        show_repetitions: bool = True,
    ) -> None:
        self._term = terminal or Terminal()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
        self._name_width = name_width
        self._show_repetitions = show_repetitions
        # The terminal width, and column header -> width in display order; set
        # per run, so a piped run doesn't probe the terminal for every row.
        self._width = 0
        self._widths: dict[str, int] = {}
        self._header_printed = False
        # Each repeated benchmark's rows, for the summary at the end.
        self._repeated: dict[str, list[BenchmarkResult]] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._repeated = {}
        self._width = self._term.width
        self._print_banner(context)
        widths = {"Iters": 12, "Real": 10, "CPU": 10}
        if self._show_memory:
            widths["Peak Mem"] = 10
        # The name takes what's left, or what its longest name needs, but at least
        # 20 columns even if rows then overflow.
        spare = self._width - sum(widths.values()) - len(widths) * len(_COL_SEP)
        if self._name_width is not None:
            spare = min(spare, self._name_width)
        self._widths = {"Benchmark": max(20, spare), **widths}
        self._header_printed = False
        # Without repetitions, every row may go to the summary: no empty table.
        if self._show_repetitions:
            self._print_header()

    def _print_header(self) -> None:
        line = self._join(list(self._widths))
        color = self._term.color
        self._term.print(sgr(line, "bold", enabled=color))
        self._term.print(sgr("─" * len(line), "dim", enabled=color))
        self._header_printed = True

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        for row in runs:
            repeated = row["repetitions"] > 1 and not row["skipped"]
            if repeated:
                self._repeated.setdefault(row["benchmark"], []).append(row)
            if self._show_repetitions or not repeated:
                if not self._header_printed:
                    self._print_header()
                self._print_row(row)

    def finalize(self) -> None:
        """Sum up the repeated benchmarks, in the order they were registered:
        random interleaving reports them in the order they finish."""
        groups = sorted(
            (rows for rows in self._repeated.values() if len(rows) > 1),
            key=lambda rows: (rows[0]["family_index"], rows[0]["per_family_instance_index"]),
        )
        self._repeated = {}
        if not groups:
            return
        table = Table(title="Median of repetitions")
        table.add_column("Benchmark", flex=True)
        for header in ("Reps", "Real", "± stddev", "CV", "CPU"):
            table.add_column(header, justify="right")
        for rows in groups:
            real, unit = [row["real_time"] for row in rows], rows[0]["time_unit"]
            mean, stddev = statistics.fmean(real), statistics.stdev(real)
            table.add_row(
                rows[0]["benchmark"],
                str(len(rows)),
                _fmt_time(statistics.median(real), unit),
                _fmt_time(stddev, unit),
                f"{stddev / abs(mean):.2%}" if mean else "-",
                _fmt_time(statistics.median(row["cpu_time"] for row in rows), unit),
            )
        self._term.print()
        for line in table.render(self._width, color=self._term.color):
            self._term.print(line)

    def _print_banner(self, context: dict[str, Any]) -> None:
        session = context.get("session", {})
        machine = context.get("context", {})
        scaling = machine.get("cpu_scaling_enabled")
        fields = {
            "host": session.get("host") or "?",
            "cpus": machine.get("num_cpus", "?"),
            "scaling": "enabled" if scaling is True else "disabled" if scaling is False else "?",
        }
        color = self._term.color
        self._term.print(
            f"{sgr('mew', 'bold', enabled=color)} {sgr('·', 'dim', enabled=color)} "
            + " ".join(f"{k}={sgr(str(v), 'cyan', enabled=color)}" for k, v in fields.items())
        )

    def _join(self, cells: list[str]) -> str:
        """Pad the name on the right and the numbers on the left, to their columns."""
        widths = self._widths.values()
        padded = [
            cell.ljust(width) if i == 0 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(cells, widths, strict=True))
        ]
        return _COL_SEP.join(padded)

    def _print_row(self, row: BenchmarkResult) -> None:
        width = self._widths["Benchmark"]
        color = self._term.color
        # Canonical `file.py::f[label]` form, matching `mew compare`, left-ellipsized
        # to keep the disambiguating tail.
        full_name = row["benchmark"]
        name = _truncate_left(full_name, width)

        # Skipped rows carry no timing, so the dimmed reason fills the timing
        # columns. Its lines (a traceback has several) wrap, keeping their indent
        # and every character, since a cut path would no longer locate anything.
        if row["skipped"]:
            lead = name.ljust(width) + _COL_SEP
            avail = max(10, self._width - len(lead))
            lines: list[str] = []
            for text in (row["skip_message"] or "skipped").splitlines():
                body = text.lstrip(" ")
                pad = text[: len(text) - len(body)][: avail // 2]
                lines += textwrap.wrap(
                    body, avail, initial_indent=pad, subsequent_indent=pad, break_on_hyphens=False
                ) or [""]
            for i, line in enumerate(lines):
                prefix = " " * len(lead) if i else lead
                self._term.print(sgr(prefix + line, "dim", enabled=color))
            return

        unit = row["time_unit"]
        real, cpu_time = _fmt_time(row["real_time"], unit), _fmt_time(row["cpu_time"], unit)
        cells = [name, f"{row['iterations']:,}", real, cpu_time]
        if self._show_memory:
            mem = row.get("memory")
            cells.append(_fmt_bytes(mem["peak_bytes"]) if mem else "-")
        self._term.print(self._join(cells))

        # A family case's own label is part of its name already.
        label = row["label"]
        details = [label] if label and f"[{label}]" not in full_name else []
        details += _fmt_counters(row)
        if self._show_cpu:
            details += _fmt_profile(row.get("cpu_profile"))
        for line in fill(details, self._width, indent="  ", hang="    ", sep=" · "):
            self._term.print(sgr(line, "dim", enabled=color))


class Fanout:
    """Pass every callback on to several reporters, in order.

    :func:`mew.run` wraps a list of reporters in one. A reporter that raises
    stops the run, as it would alone.

    Parameters
    ----------
    reporters : list[Reporter]
        The reporters, called in list order.
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
