"""Minimal terminal rendering, so the runtime needs no third-party dependency.

Replaces the handful of rich primitives mew used (``Console``, ``Text``,
``Table``, ``markup.escape``). Scope is only what mew renders: the streamed
results table (:class:`~mew.reporter.RichReporter`), the comparison table, and
colorized ``--help``. There is no markup language; a cell carries its style
out of band, so ``[label]`` needs no escaping. Long cells in the one flexible
column are left-ellipsized; there is no wrapping or East-Asian width handling.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

# Style name -> ANSI SGR code (only the styles mew uses).
_SGR = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}

# A styled run of text. ``style`` is a key of _SGR, or None for unstyled.
Span = tuple[str, str | None]
# A table cell: plain text, or a sequence of differently-styled spans.
Cell = str | list[Span]

_COL_SEP = " │ "


def sgr(text: str, *styles: str, enabled: bool = True) -> str:
    """Wrap ``text`` in ANSI codes for ``styles``; a no-op when disabled or empty."""
    names = [s for s in styles if s]
    if not enabled or not names or not text:
        return text
    codes = ";".join(_SGR[s] for s in names)
    return f"\x1b[{codes}m{text}\x1b[0m"


def terminal_width(default: int = 80) -> int:
    import shutil

    return shutil.get_terminal_size((default, 24)).columns


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


class Terminal:
    """A thin stdout wrapper with a known width and a color gate.

    Parameters
    ----------
    file : TextIO, optional
        Sink to write to. Defaults to ``sys.stdout``.
    width : int, optional
        Fixed column count. Defaults to the detected terminal width per print.
    color : bool, optional
        Force ANSI on/off. Defaults to auto-detection from ``file``.
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
        return self._width if self._width is not None else terminal_width()

    @property
    def color(self) -> bool:
        return self._color

    def print(self, obj: object = "") -> None:
        """Write ``obj`` and a newline. A :class:`Table` renders to multiple lines."""
        lines = obj.render(self.width, color=self._color) if isinstance(obj, Table) else [str(obj)]
        for line in lines:
            self._file.write(line + "\n")


def _visible_len(cell: Cell) -> int:
    if isinstance(cell, str):
        return len(cell)
    return sum(len(t) for t, _ in cell)


def _truncate_left(text: str, width: int) -> str:
    """Keep the right side (the disambiguating suffix), prefixing an ellipsis."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return "…" + text[-(width - 1) :]


def _format_cell(cell: Cell, width: int, justify: str, color: bool) -> str:
    if isinstance(cell, str):
        text = _truncate_left(cell, width)
        pad = width - len(text)
        body = text
    else:  # styled spans: only ever in fixed columns, so never truncated
        body = "".join(sgr(t, s, enabled=color) if s else t for t, s in cell)
        pad = max(0, width - _visible_len(cell))
    return " " * pad + body if justify == "right" else body + " " * pad


class Table:
    """A simple column table: fixed columns sized to content, one flexible column.

    The flexible column (``flex=True``, e.g. the benchmark name) absorbs leftover
    width and is left-ellipsized when narrowed; fixed columns are sized to their
    widest cell and right/left justified.
    """

    def __init__(self, title: str | None = None) -> None:
        self.title = title
        self._headers: list[str] = []
        self._justify: list[str] = []
        self._flex: list[bool] = []
        self._rows: list[list[Cell]] = []

    def add_column(self, header: str, *, justify: str = "left", flex: bool = False) -> None:
        self._headers.append(header)
        self._justify.append(justify)
        self._flex.append(flex)

    def add_row(self, *cells: Cell) -> None:
        self._rows.append(list(cells))

    def render(self, width: int, *, color: bool = True) -> list[str]:
        widths = [len(h) for h in self._headers]
        for row in self._rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _visible_len(cell))

        # At most one flex column (the benchmark name); it absorbs leftover width.
        flex = next((i for i, f in enumerate(self._flex) if f), None)
        if flex is not None:
            overhead = (len(self._headers) - 1) * len(_COL_SEP)
            fixed = sum(w for i, w in enumerate(widths) if i != flex)
            avail = max(1, width - fixed - overhead)
            widths[flex] = max(len(self._headers[flex]), min(widths[flex], avail))

        lines: list[str] = []
        if self.title:
            lines.append(sgr(self.title, "bold", enabled=color))
        header = _COL_SEP.join(
            sgr(_format_cell(h, w, j, False), "bold", enabled=color)
            for h, w, j in zip(self._headers, widths, self._justify, strict=True)
        )
        lines.append(header)
        lines.append(
            sgr("─" * (sum(widths) + (len(widths) - 1) * len(_COL_SEP)), "dim", enabled=color)
        )
        for row in self._rows:
            lines.append(
                _COL_SEP.join(
                    _format_cell(c, w, j, color)
                    for c, w, j in zip(row, widths, self._justify, strict=True)
                )
            )
        return lines
