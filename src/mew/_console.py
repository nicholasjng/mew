"""Minimal terminal rendering: ANSI styles, column tables, line filling, and
the formatting of times, byte counts and counters as text.

It needs no third-party dependency. There is no markup language; a cell carries
its style out of band, so ``[label]`` needs no escaping. Long cells in a
table's one flexible column are left-ellipsized, and :func:`fill` lays out
parts on lines without splitting any. There is no East-Asian width handling.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any, TextIO

# Style name -> ANSI SGR code (only the styles mew uses).
_SGR = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

# A styled run of text. ``style`` is a key of _SGR, or None for unstyled.
Span = tuple[str, str | None]
# A table cell: plain text, or a sequence of differently-styled spans.
Cell = str | list[Span]

COL_SEP = " │ "
# Fewest columns a narrowed name keeps: below that it no longer identifies anything.
_FLEX_FLOOR = 20


def error(message: str) -> None:
    """Print ``mew: error: <message>`` to stderr. A ``hint:`` line in it prints as is."""
    print(f"mew: error: {message}", file=sys.stderr)


def warn(message: str) -> None:
    """Print ``mew: warning: <message>`` to stderr. A ``hint:`` line in it prints as is."""
    print(f"mew: warning: {message}", file=sys.stderr)


def fill(parts: list[str], width: int, *, indent: str, hang: str, sep: str) -> list[str]:
    """Lay ``parts`` out on lines no wider than ``width``, never splitting one.

    The first line starts with ``indent``, the others with ``hang``; a part too
    long for a line of its own is right-ellipsized.
    """
    lines: list[str] = []
    for part in parts:
        if lines and len(lines[-1]) + len(sep) + len(part) <= width:
            lines[-1] += sep + part
        else:
            lead = hang if lines else indent
            lines.append(lead + truncate_right(part, max(1, width - len(lead))))
    return lines


def sgr(text: str, *styles: str | None, enabled: bool = True) -> str:
    """Wrap ``text`` in ANSI codes for ``styles``; a no-op when disabled or empty."""
    names = [s for s in styles if s]
    if not enabled or not names or not text:
        return text
    codes = ";".join(_SGR[s] for s in names)
    return f"\x1b[{codes}m{text}\x1b[0m"


def color_enabled(stream: TextIO) -> bool:
    """Whether to emit ANSI: honor ``NO_COLOR`` / ``FORCE_COLOR``, else TTY detection."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _terminal_width() -> int:
    """Return the width of the screen in columns.

    The width is ``COLUMNS``, else the width of the terminal on stdout, stderr or
    ``/dev/tty``, so piped output still fits the screen, else 80.
    """
    import shutil

    if columns := shutil.get_terminal_size((0, 0)).columns:  # COLUMNS, else stdout
        return columns
    try:
        return os.get_terminal_size(2).columns or 80
    except OSError:
        pass
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:  # no controlling terminal, as in CI, or on Windows
        return 80
    try:
        return os.get_terminal_size(fd).columns or 80
    except OSError:
        return 80
    finally:
        os.close(fd)


class Terminal:
    """An output stream with a width in columns and a color setting.

    Unless given, the width follows the terminal at each print, and colors
    follow the stream and the ``NO_COLOR`` and ``FORCE_COLOR`` variables.

    Parameters
    ----------
    file : TextIO, optional
        The stream to write to; ``sys.stdout`` by default.
    width : int, optional
        A fixed width in columns. By default, the width is read at each print:
        ``COLUMNS``, else the width of the terminal on stdout, stderr or
        ``/dev/tty``, else 80.
    color : bool, optional
        Whether to print ANSI colors. By default, colors are off when
        ``NO_COLOR`` is set, on when ``FORCE_COLOR`` is set, and otherwise on
        when ``file`` is a terminal.
    """

    def __init__(
        self,
        *,
        file: TextIO | None = None,
        width: int | None = None,
        color: bool | None = None,
    ) -> None:
        self._file = file if file is not None else sys.stdout
        self._width = width
        self._color = color_enabled(self._file) if color is None else color

    @property
    def width(self) -> int:
        """The width in columns, fixed or else the terminal's current one."""
        return self._width if self._width is not None else _terminal_width()

    @property
    def color(self) -> bool:
        """Whether this terminal prints ANSI colors."""
        return self._color

    def print(self, obj: object = "") -> None:
        """Write ``obj`` and a newline.

        Parameters
        ----------
        obj : object, default ""
            What to write, as ``str(obj)``. mew's own tables lay themselves out
            over several lines, within :attr:`width`.
        """
        lines = obj.render(self.width, color=self._color) if isinstance(obj, Table) else [str(obj)]
        for line in lines:
            self._file.write(line + "\n")


def _visible_len(cell: Cell) -> int:
    if isinstance(cell, str):
        return len(cell)
    return sum(len(t) for t, _ in cell)


def truncate_left(text: str, width: int) -> str:
    """Keep the right side (the disambiguating suffix), prefixing an ellipsis."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return "…" + text[-(width - 1) :]


def truncate_right(text: str, width: int) -> str:
    """Keep the left side, appending an ellipsis."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _format_cell(cell: Cell, width: int, justify: str, color: bool) -> str:
    if isinstance(cell, str):
        text = truncate_left(cell, width)
        pad = width - len(text)
        body = text
    else:  # styled spans: only ever in fixed columns, so never truncated
        body = "".join(sgr(t, s, enabled=color) for t, s in cell)
        pad = max(0, width - _visible_len(cell))
    return " " * pad + body if justify == "right" else body + " " * pad


class Table:
    """A simple column table: fixed columns sized to content, one flexible column.

    The flexible column (``flex=True``, e.g. the benchmark name) absorbs leftover
    width and is left-ellipsized when narrowed, but keeps ``_FLEX_FLOOR`` columns
    even if the table then overflows; fixed columns are sized to their widest cell
    and right/left justified.
    """

    def __init__(self, title: str | None = None) -> None:
        self.title = title
        self._headers: list[str] = []
        self._justify: list[str] = []
        self._flex: int | None = None  # the flexible column's index
        self._rows: list[list[Cell]] = []

    def add_column(self, header: str, *, justify: str = "left", flex: bool = False) -> None:
        if flex:
            if self._flex is not None:
                raise ValueError("a table has at most one flexible column")
            self._flex = len(self._headers)
        self._headers.append(header)
        self._justify.append(justify)

    def add_row(self, *cells: Cell) -> None:
        self._rows.append(list(cells))

    def render(self, width: int, *, color: bool = True) -> list[str]:
        widths = [len(h) for h in self._headers]
        for row in self._rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _visible_len(cell))

        if (flex := self._flex) is not None:
            overhead = (len(self._headers) - 1) * len(COL_SEP)
            fixed = sum(w for i, w in enumerate(widths) if i != flex)
            avail = width - fixed - overhead
            floor = min(widths[flex], max(len(self._headers[flex]), _FLEX_FLOOR))
            widths[flex] = max(floor, min(widths[flex], avail))

        lines: list[str] = []
        if self.title:
            lines.append(sgr(self.title, "bold", enabled=color))
        header = COL_SEP.join(
            sgr(_format_cell(h, w, j, False), "bold", enabled=color)
            for h, w, j in zip(self._headers, widths, self._justify, strict=True)
        )
        lines.append(header)
        lines.append(
            sgr("─" * (sum(widths) + (len(widths) - 1) * len(COL_SEP)), "dim", enabled=color)
        )
        lines.extend(
            COL_SEP.join(
                _format_cell(c, w, j, color)
                for c, w, j in zip(row, widths, self._justify, strict=True)
            )
            for row in self._rows
        )
        return lines


NS_PER_UNIT = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}


def scale_time(ns: float) -> tuple[float, str]:
    """Rescale nanoseconds to the largest of s/ms/us/ns that keeps the value >= 1.

    For example, ``6995135790.99`` becomes ``(7.00, "s")``.
    """
    for threshold, unit in ((1e9, "s"), (1e6, "ms"), (1e3, "us")):
        if abs(ns) >= threshold:
            return ns / threshold, unit
    return ns, "ns"


def fmt_time(ns: float) -> str:
    """Nanoseconds as ``7.00 s``, in the unit :func:`scale_time` picks."""
    scaled, unit = scale_time(ns)
    return f"{scaled:.2f} {unit}"


def fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


_BIG_SI = ("k", "M", "G", "T", "P", "E", "Z", "Y")
_BIG_IEC = ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi", "Yi")
_SMALL_SI = ("m", "u", "n", "p", "f", "a", "z", "y")


def human_readable(value: float, one_k: int, exponent: bool) -> str:
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


def fmt_counter(value: float, fmt: Mapping[str, Any] | None) -> str:
    """A counter value as Google Benchmark's console shows it, e.g. ``1.2Gi/s``.

    ``fmt`` is the counter's ``format`` record from a result row, if it has one.
    """
    fmt = fmt or {}
    text = human_readable(value, fmt.get("one_k", 1000), fmt.get("exponent", False))
    if fmt.get("rate"):
        text += "s" if fmt.get("inverted") else "/s"
    return text


def overflow(total: int, shown: int) -> str:
    """`` (+N more)`` when a message previewed only ``shown`` of ``total`` items."""
    return f" (+{total - shown} more)" if total > shown else ""
