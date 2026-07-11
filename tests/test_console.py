"""Tests for `mew._console`'s truncation helpers and `Table` rendering.

These are the primitives behind the "reporter: properly truncate rows" fix
(commit df346b6); they had no direct test coverage before this file.
"""

from __future__ import annotations

from mew._console import Table, Terminal, _truncate_left, _truncate_right


def test_truncate_left_no_op_when_text_fits() -> None:
    assert _truncate_left("short", 10) == "short"
    assert _truncate_left("exact", 5) == "exact"


def test_truncate_left_keeps_suffix_with_ellipsis_prefix() -> None:
    assert _truncate_left("bench_the_actual_function", 10) == "…_function"
    assert len(_truncate_left("bench_the_actual_function", 10)) == 10


def test_truncate_left_width_one_or_less_has_no_room_for_ellipsis() -> None:
    # Too narrow to fit an ellipsis at all: fall back to a bare slice.
    assert _truncate_left("abcdef", 1) == "a"
    assert _truncate_left("abcdef", 0) == ""


def test_truncate_right_no_op_when_text_fits() -> None:
    assert _truncate_right("short", 10) == "short"
    assert _truncate_right("exact", 5) == "exact"


def test_truncate_right_keeps_prefix_with_ellipsis_suffix() -> None:
    assert _truncate_right("some-long-variant-name", 10) == "some-long…"
    assert len(_truncate_right("some-long-variant-name", 10)) == 10


def test_truncate_right_width_one_or_less_has_no_room_for_ellipsis() -> None:
    assert _truncate_right("abcdef", 1) == "a"
    assert _truncate_right("abcdef", 0) == ""


def test_table_flex_column_left_ellipsizes_when_width_forces_narrowing() -> None:
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Iters", justify="right")
    table.add_row("benchmarks/some/deeply/nested/bench_the_actual_function", "1,000")

    lines = table.render(width=40, color=False)
    data_line = lines[-1]
    assert "…" in data_line
    assert "bench_the_actual_function" in data_line


def test_table_fixed_column_sizes_to_widest_cell_and_is_never_truncated() -> None:
    # Only the flex column is left-ellipsized (via `_format_cell`'s
    # unconditional `_truncate_left`); fixed columns instead grow to fit
    # their widest cell, so their content survives even a very wide value.
    # (`RichReporter`'s variant/label/hottest-frame right-truncation is a
    # separate mechanism applied before the cell reaches this table at all.)
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Variant", justify="left")
    table.add_row("b", "short")
    table.add_row("b", "x" * 30)

    lines = table.render(width=200, color=False)
    assert "x" * 30 in lines[-1]


def test_table_header_and_row_count_matches_columns() -> None:
    table = Table(title="My Table")
    table.add_column("A")
    table.add_column("B", justify="right")
    table.add_row("1", "2")

    lines = table.render(width=80, color=False)
    assert lines[0] == "My Table"
    assert "A" in lines[1] and "B" in lines[1]
    assert lines[-1].strip().endswith("2")


def test_table_render_with_color_wraps_header_in_ansi() -> None:
    table = Table()
    table.add_column("A")
    table.add_row("x")

    plain = table.render(width=80, color=False)
    colored = table.render(width=80, color=True)
    assert "\x1b[" not in plain[0]
    assert "\x1b[" in colored[0]


def test_table_styled_span_cell_is_never_truncated() -> None:
    # Styled-span cells (list[Span]) are documented as "only ever in fixed
    # columns, so never truncated" — verify that holds even when narrower
    # than the rendered content.
    table = Table()
    table.add_column("Benchmark", flex=True)
    table.add_column("Delta", justify="right")
    table.add_row("b", [("+123.45%", "red")])

    lines = table.render(width=20, color=True)
    assert "+123.45%" in lines[-1]


def test_terminal_print_renders_table_lines() -> None:
    import io

    buf = io.StringIO()
    term = Terminal(file=buf, width=40, color=False)
    table = Table()
    table.add_column("A")
    table.add_row("x")
    term.print(table)
    assert buf.getvalue() == "A\n" + "─" * 1 + "\n" + "x\n"
