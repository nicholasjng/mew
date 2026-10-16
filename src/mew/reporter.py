"""Reporter protocol and built-in reporters."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, TextIO, cast, runtime_checkable

from mew._console import _COL_SEP, Terminal, _truncate_left, _truncate_right, sgr
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


def _fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def _fmt_function(cpu: Mapping[str, Any] | None) -> str:
    """``work (bench.py:6)`` from a :class:`~mew.cpu.TopFunction`, as far as it goes."""
    top = cpu.get("top_function") if cpu else None
    if not isinstance(top, Mapping):  # A custom profiler's summary.
        return str(top) if top else "-"
    where = ":".join(str(part) for part in (top["file"], top["line"]) if part is not None)
    return f"{top['function']} ({where})" if where else top["function"]


def _fmt_instruction(cpu: Mapping[str, Any] | None, width: int) -> str:
    """``BINARY_OP 62% · 62% generic``, cutting the opcode name to keep the shares."""
    if not cpu or not cpu.get("top_instructions"):
        return "-"
    top = cpu["top_instructions"][0]
    name = top["instruction"]
    shares = f" {top['share']:.0%}"
    if (generic := cpu.get("unspecialized_share") or 0.0) >= 0.005:
        shares += f" · {generic:.0%} generic"
    return _truncate_right(name, max(1, width - len(shares))) + shares


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


def _fmt_counters(row: BenchmarkResult) -> str:
    """Counters as Google Benchmark's console shows them: ``name=1.2Gi/s ...``."""
    ratio = row.get("aggregate_unit") == "percentage"
    parts = []
    for counter in row["counters"]:
        value, fmt = counter["value"], counter.get("format", {})
        if ratio:  # e.g. the `cv` aggregate of a counter
            text = f"{100 * value:.2f}%"
        else:
            text = _human_readable(value, fmt.get("one_k", 1000), fmt.get("exponent", False))
            if fmt.get("rate"):
                text += "s" if fmt.get("inverted") else "/s"
        parts.append(f"{counter['name']}={text}")
    return " ".join(parts)


def canonical_row_name(row: Mapping[str, Any]) -> str:
    """The display name of a stored row: its ``benchmark`` plus any aggregate suffix."""
    aggregate = row.get("aggregate_name")
    return f"{row['benchmark']}_{aggregate}" if aggregate else row["benchmark"]


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


# RichReporter columns whose cells pad on the right; the rest are right-aligned numbers.
_LEFT_ALIGNED = frozenset({"Benchmark", "Label", "Hottest Frame", "Hottest Instruction"})


class RichReporter:
    """Print mew's console table, a row per result as it arrives.

    The header prints before the first result, so the optional columns are
    chosen up front.

    Parameters
    ----------
    terminal : Terminal, optional
        Where to print; defaults to stdout, colored when it is a terminal.
    show_memory : bool, default False
        Add a ``Peak Mem`` column.
    show_cpu : bool, default False
        Add ``Samples`` / ``Hottest Frame`` columns.
    show_instructions : bool, default False
        Add a ``Hottest Instruction`` column, e.g. ``BINARY_OP 62% · 62% generic``,
        with ``show_cpu`` where :func:`mew.cpu.reads_instructions`.
    show_label : bool, default False
        Add a ``Label`` column with each family case's label, which a truncated
        name can hide.
    """

    def __init__(
        self,
        *,
        terminal: Terminal | None = None,
        show_memory: bool = False,
        show_cpu: bool = False,
        show_instructions: bool = False,
        show_label: bool = False,
    ) -> None:
        self._term = terminal or Terminal()
        self._show_memory = show_memory
        self._show_cpu = show_cpu
        self._show_instructions = show_instructions
        self._show_label = show_label
        # Column header -> width, in display order; set once the terminal width is known.
        self._widths: dict[str, int] = {}

    def report_context(self, context: dict[str, Any]) -> None:
        self._print_banner(context)
        widths = {"Label": 20} if self._show_label else {}
        widths |= {"Iters": 12, "Real": 14, "CPU": 14}
        if self._show_memory:
            widths["Peak Mem"] = 10
        if self._show_cpu:
            widths |= {"Samples": 9, "Hottest Frame": 30}
        if self._show_instructions:
            widths["Hottest Instruction"] = 30
        # Name column takes whatever's left; floor at 30 even if it overflows.
        spare = self._term.width - sum(widths.values()) - len(widths) * len(_COL_SEP)
        self._widths = {"Benchmark": max(30, spare), **widths}
        line = self._join(list(self._widths))
        color = self._term.color
        self._term.print(sgr(line, "bold", enabled=color))
        self._term.print(sgr("─" * len(line), "dim", enabled=color))

    def report_runs(self, runs: list[BenchmarkResult]) -> None:
        for row in runs:
            self._print_row(row)

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
        """Pad each cell to its column; a cell past the last column (counters) runs free."""
        padded = [
            cell.ljust(width) if header in _LEFT_ALIGNED else cell.rjust(width)
            for cell, (header, width) in zip(cells, self._widths.items(), strict=False)
        ]
        return _COL_SEP.join(padded + cells[len(self._widths) :])

    def _print_row(self, row: BenchmarkResult) -> None:
        w = self._widths
        # Canonical `file.py::f[label]` form, matching `mew compare`, left-ellipsized
        # to keep the disambiguating tail.
        name = _truncate_left(canonical_row_name(row), w["Benchmark"])

        # Skipped rows carry no timing, so show the dimmed reason instead.
        if row["skipped"]:
            reason = row["skip_message"] or "skipped"
            line = f"{name.ljust(w['Benchmark'])}{_COL_SEP}{reason}"
            self._term.print(sgr(line, "dim", enabled=self._term.color))
            return

        cells = [name]
        if self._show_label:
            cells.append(_truncate_right(row["label"], w["Label"]))
        if row.get("aggregate_unit") == "percentage":
            # A ratio such as `cv`, shown as a percentage like GB's console.
            real, cpu_time = f"{row['real_time']:.2%}", f"{row['cpu_time']:.2%}"
        else:
            unit = row["time_unit"]
            real, cpu_time = f"{row['real_time']:.2f} {unit}", f"{row['cpu_time']:.2f} {unit}"
        cells += [f"{row['iterations']:,}", real, cpu_time]
        if self._show_memory:
            mem = row.get("memory")
            cells.append(_fmt_bytes(mem["peak_bytes"]) if mem else "-")
        cpu = row.get("cpu_profile")
        if self._show_cpu:
            cells.append(f"{cpu['sample_count']:,}" if cpu else "-")
            cells.append(_truncate_right(_fmt_function(cpu), w["Hottest Frame"]))
        if self._show_instructions:
            cells.append(_fmt_instruction(cpu, w["Hottest Instruction"]))
        if row["counters"]:
            cells.append(_fmt_counters(row))
        self._term.print(self._join(cells))


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
