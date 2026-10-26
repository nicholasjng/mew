"""Tests for dotted paths."""

from __future__ import annotations

import pytest

from mew._paths import (
    format_path,
    parse_path,
    set_path,
    split_assignment,
)


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("a", ("a",)),
        ("context.vcs.commit", ("context", "vcs", "commit")),
        ('context."a.b"', ("context", "a.b")),
        ('"say ""hi"""', ('say "hi"',)),
        ('"a\nb".c', ("a\nb", "c")),
    ],
)
def test_paths_nest_at_each_unquoted_dot(text: str, path: tuple[str, ...]) -> None:
    """A dot nests, a quoted key keeps its dots, and formatting round-trips."""
    assert parse_path(text) == path
    assert parse_path(format_path(path)) == path


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty key"),
        ("a.", "empty key"),
        (".a", "empty key"),
        ('""', "empty key"),
        ('"a', "malformed path"),
        ('"a"b', "malformed path"),
        ("a\nb", "malformed path"),
    ],
)
def test_malformed_paths_raise(text: str, match: str) -> None:
    """An empty key, or a quote that doesn't wrap a whole key, is a `ValueError`."""
    with pytest.raises(ValueError, match=match):
        parse_path(text)


def test_assignment_splits_at_the_first_unquoted_equals() -> None:
    """`<path>=<value>` splits at the first `=` outside a quoted key, so the value may hold `=`."""
    assert split_assignment("a.b=c=d") == (("a", "b"), "c=d")
    assert split_assignment('"x=y"=z') == (("x=y",), "z")


def test_set_path_copies_the_mappings_it_changes() -> None:
    """Setting below a caller's mapping leaves that mapping as it was."""
    inner = {"version": "1"}
    outer = {"jax": inner, "n": 3}
    set_path(outer, ("jax", "backend"), "gpu")
    set_path(outer, ("n", "x"), "y")  # a value in the way is replaced
    assert outer == {"jax": {"version": "1", "backend": "gpu"}, "n": {"x": "y"}}
    assert inner == {"version": "1"}
