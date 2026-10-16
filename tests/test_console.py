"""`mew._console` truncation and fill helpers and `Table` rendering."""

from __future__ import annotations

import os
import shutil

import pytest

from mew._console import Table, Terminal, _terminal_width, _truncate_left, _truncate_right, fill


@pytest.mark.parametrize(
    ("truncate", "text", "width", "expected"),
    [
        (_truncate_left, "short", 10, "short"),
        (_truncate_left, "exact", 5, "exact"),
        (_truncate_left, "bench_the_actual_function", 10, "…_function"),
        # Too narrow for an ellipsis: a bare slice.
        (_truncate_left, "abcdef", 1, "a"),
        (_truncate_left, "abcdef", 0, ""),
        (_truncate_right, "short", 10, "short"),
        (_truncate_right, "exact", 5, "exact"),
        (_truncate_right, "some-long-case-label", 10, "some-long…"),
        (_truncate_right, "abcdef", 1, "a"),
        (_truncate_right, "abcdef", 0, ""),
    ],
)
def test_truncate(truncate, text, width, expected) -> None:
    assert truncate(text, width) == expected
    assert len(truncate(text, width)) <= max(width, 0)


def test_table_flex_column_left_ellipsizes_when_width_forces_narrowing() -> None:
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Iters", justify="right")
    table.add_row("benchmarks/some/deeply/nested/bench_the_actual_function", "1,000")

    lines = table.render(width=40, color=False)
    data_line = lines[-1]
    assert "…" in data_line
    assert "bench_the_actual_function" in data_line


def test_table_flex_column_keeps_a_floor_rather_than_vanishing() -> None:
    t = Table()
    t.add_column("Benchmark", flex=True)
    t.add_column("Value", justify="right")
    t.add_row("bench.py::bench_with_a_long_name[n=1]", "1.00 us")
    # No room at all: the name keeps 20 columns, overflowing the width.
    name = t.render(width=10, color=False)[-1].split(" │ ")[0]
    assert len(name) == 20 and name.endswith("_long_name[n=1]")


def test_fill_lays_out_whole_parts_within_the_width() -> None:
    parts = ["alpha=1", "beta=22", "gamma=333", "delta=4444"]
    lines = fill(parts, 24, indent="  ", hang="    ", sep=" · ")
    assert lines == ["  alpha=1 · beta=22", "    gamma=333", "    delta=4444"]
    assert all(len(line) <= 24 for line in lines)
    # A part too long for a line of its own is cut; nothing at all is no lines.
    assert fill(["x" * 30], 12, indent="", hang="  ", sep=" ") == ["xxxxxxxxxxx…"]
    assert fill([], 80, indent="  ", hang="    ", sep=" · ") == []


def test_table_fixed_column_sizes_to_widest_cell_and_is_never_truncated() -> None:
    # Only the flex column is ellipsized; fixed columns grow to fit their widest cell.
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Variant", justify="left")
    table.add_row("b", "short")
    table.add_row("b", "x" * 30)

    # Far narrower than the content: only the flex column may give way.
    lines = table.render(width=10, color=False)
    assert "x" * 30 in lines[-1]


def test_table_render_with_color_wraps_header_in_ansi() -> None:
    table = Table()
    table.add_column("A")
    table.add_row("x")

    plain = table.render(width=80, color=False)
    colored = table.render(width=80, color=True)
    assert "\x1b[" not in plain[0]
    assert "\x1b[" in colored[0]


def test_table_styled_span_cell_is_never_truncated() -> None:
    # Styled-span cells only appear in fixed columns, so must never be truncated.
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Delta", justify="right")
    table.add_row("b", [("+123.45%", "red")])

    lines = table.render(width=5, color=True)
    assert "+123.45%" in lines[-1]


def test_terminal_print_renders_table_lines() -> None:
    import io

    buf = io.StringIO()
    term = Terminal(file=buf, width=40, color=False)
    table = Table(title="My Table")
    table.add_column("A")
    table.add_row("x")
    term.print(table)
    assert buf.getvalue() == "My Table\n" + "A\n" + "─" * 1 + "\n" + "x\n"


@pytest.mark.parametrize(
    ("terminals", "width"),
    [
        ({1: 120}, 120),  # stdout is the terminal
        ({2: 132}, 132),  # stdout piped (`| tee`), stderr still the terminal
        ({"tty": 140}, 140),  # both redirected: the controlling terminal
        ({}, 80),  # no terminal at all, as in CI
    ],
)
def test_terminal_width_asks_stdout_then_stderr_then_the_tty(monkeypatch, terminals, width):
    monkeypatch.delenv("COLUMNS", raising=False)
    tty_fd = 99

    def get_terminal_size(fd=None):
        key = "tty" if fd == tty_fd else fd
        if key in terminals:
            return os.terminal_size((terminals[key], 24))
        raise OSError("not a terminal")

    def open_tty(path, flags):
        if "tty" not in terminals:
            raise OSError("no controlling terminal")
        return tty_fd

    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size((terminals.get(1, fallback[0]), 24)),
    )
    monkeypatch.setattr(os, "get_terminal_size", get_terminal_size)
    monkeypatch.setattr(os, "open", open_tty)
    monkeypatch.setattr(os, "close", lambda fd: None)
    assert _terminal_width() == width
