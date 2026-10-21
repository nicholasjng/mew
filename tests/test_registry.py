"""Registry behavior."""

from __future__ import annotations

import dataclasses

import pytest

from mew._registry import Entry, Registry, compile_name_filter, narrow_entry

_LABELS = ["n=1", "n=10", "n=100"]


def _family(name: str = "f.py::bench_fam") -> Entry:
    return Entry(name=name, fn=lambda s: None, case_labels=_LABELS)


def _names(entries: list[Entry]) -> list[str]:
    return [e.name for e in entries]


@pytest.mark.parametrize(
    ("labels", "pattern", "cases"),
    [
        # A plain benchmark is all or nothing.
        (None, "bench_fam", None),
        (None, "nope", []),
        # The family's own name, or a pattern every case matches, needn't narrow.
        (_LABELS, "bench_fam", None),
        (_LABELS, r"n=\d+\]", None),
        # `name[label]` is addressable.
        (_LABELS, r"bench_fam\[n=100\]", [2]),
        (_LABELS, r"n=1\]|n=100\]", [0, 2]),  # labels n=1 and n=100, not n=10
    ],
)
def test_narrow_entry(labels, pattern, cases):
    """`narrow_entry` returns a copy limited to the matching cases, the whole entry, or `None`."""
    entry = Entry(name="f.py::bench_fam", fn=lambda s: None, case_labels=labels)
    narrowed = narrow_entry(entry, [compile_name_filter(pattern)])
    # No case left drops the entry; every case keeps it whole (`cases=None`).
    assert narrowed == (None if cases == [] else dataclasses.replace(entry, cases=cases))
    assert entry.cases is None  # a narrowed view is a copy


def test_narrow_entry_ors_its_filters_and_narrows_views_further():
    """One `narrow_entry` call ORs its filters; narrowing a view again keeps cases both select."""
    # One call ORs its filters: cases 0 and 2.
    view = narrow_entry(_family(), [compile_name_filter("n=1]"), compile_name_filter("n=100]")])
    assert view is not None and view.cases == [0, 2]
    # Narrowing the view again keeps only cases both select, as `-k` and a
    # `path::filter` do in the CLI.
    narrowed = narrow_entry(view, [compile_name_filter("n=10")])
    assert narrowed is not None and narrowed.cases == [2]


def test_filter_pattern_is_a_regex_unless_literal():
    """`Registry.filter` reads the pattern as a regex unless `literal=True`; a bad regex raises."""
    r = Registry()
    r.add(_family())
    assert r.filter("bench_fam[n=10]") == []  # brackets are a character class
    (narrowed,) = r.filter("bench_fam[n=10]", literal=True)
    assert narrowed.cases == [1]
    with pytest.raises(ValueError, match="invalid benchmark filter pattern"):
        r.filter("bench_fam[")


def test_filter_tags_select_any_tag_and_combine_with_the_pattern():
    """`Registry.filter` keeps entries with any given tag that also match the pattern."""
    r = Registry()
    for name, tags in [("foo::a", {"io"}), ("bar::b", {"cpu"}), ("foo::c", {"io", "slow"})]:
        r.add(Entry(name=name, fn=lambda s: None, tags=frozenset(tags)))
    r.add(Entry(name="foo::d", fn=lambda s: None))
    assert _names(r.filter(tags=["io", "cpu"])) == ["foo::a", "bar::b", "foo::c"]
    assert _names(r.filter("foo", tags=["io", "cpu"])) == ["foo::a", "foo::c"]
