"""`mew._console` truncation and fill helpers, `Table` layout and the terminal width."""

from __future__ import annotations

import io
import os
import shutil

import pytest

from mew._console import Table, Terminal, _terminal_width, fill, truncate_left, truncate_right


@pytest.mark.parametrize(
    ("truncate", "width", "expected"),
    [
        (truncate_left, 6, "abcdef"),
        (truncate_left, 4, "…def"),
        (truncate_left, 1, "a"),  # too narrow for an ellipsis: a bare slice
        (truncate_right, 6, "abcdef"),
        (truncate_right, 4, "abc…"),
        (truncate_right, 1, "a"),
    ],
)
def test_truncate(truncate, width, expected) -> None:
    assert truncate("abcdef", width) == expected


def test_table_narrows_the_flex_column_keeping_its_tail() -> None:
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Iters", justify="right")
    table.add_row("benchmarks/some/deeply/nested/bench_the_actual_function", "1,000")
    assert table.render(width=40, color=False)[-1] == "…ested/bench_the_actual_function │ 1,000"


def test_table_too_narrow_keeps_fixed_columns_whole_and_a_floor_of_the_name() -> None:
    # Only the flex column gives way, and it keeps 20 columns rather than
    # vanishing, even if the table then overflows.
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Variant")
    table.add_column("Delta", justify="right")
    table.add_row("bench.py::bench_with_a_long_name[n=1]", "x" * 30, [("+123.45%", "red")])
    name, variant, delta = table.render(width=10, color=False)[-1].split(" │ ")
    assert (name, variant, delta) == ("…th_a_long_name[n=1]", "x" * 30, "+123.45%")


def test_fill_lays_out_whole_parts_within_the_width() -> None:
    parts = ["alpha=1", "beta=22", "gamma=333", "delta=4444"]
    lines = fill(parts, 24, indent="  ", hang="    ", sep=" · ")
    assert lines == ["  alpha=1 · beta=22", "    gamma=333", "    delta=4444"]
    # A part too long for a line of its own is cut; nothing at all is no lines.
    assert fill(["x" * 30], 12, indent="", hang="  ", sep=" ") == ["xxxxxxxxxxx…"]
    assert fill([], 80, indent="  ", hang="    ", sep=" · ") == []


def test_terminal_prints_a_table_line_by_line() -> None:
    buf = io.StringIO()
    table = Table(title="My Table")
    table.add_column("A")
    table.add_row("x")
    Terminal(file=buf, width=40, color=False).print(table)
    assert buf.getvalue() == "My Table\nA\n─\nx\n"
    assert table.render(40, color=True)[1] == "\x1b[1mA\x1b[0m"  # a bold header, in color


@pytest.mark.parametrize(
    ("sizes", "width"),
    [
        ({1: 120, 2: 132}, 120),  # COLUMNS, else stdout, comes first
        ({2: 132, 99: 140}, 132),  # stdout piped (`| tee`), stderr still the terminal
        ({99: 140}, 140),  # both redirected: the controlling terminal, opened as fd 99
        ({}, 80),  # no terminal at all, as in CI
    ],
)
def test_terminal_width_asks_stdout_then_stderr_then_the_tty(monkeypatch, sizes, width):
    def get_terminal_size(fd):
        if fd not in sizes:
            raise OSError("not a terminal")
        return os.terminal_size((sizes[fd], 24))

    def open_tty(path, flags):
        if 99 not in sizes:
            raise OSError("no controlling terminal")
        return 99

    # shutil answers for COLUMNS and stdout, else with the fallback it is given.
    monkeypatch.setattr(
        shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size((sizes.get(1, fallback[0]), 24)),
    )
    monkeypatch.setattr(os, "get_terminal_size", get_terminal_size)
    monkeypatch.setattr(os, "open", open_tty)
    monkeypatch.setattr(os, "close", lambda fd: None)
    assert _terminal_width() == width
