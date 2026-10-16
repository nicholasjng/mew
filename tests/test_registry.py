"""Registry behavior."""

from __future__ import annotations

import dataclasses

import pytest

from mew._registry import Entry, Registry, compile_name_filter, narrow_entry

_LABELS = ["n=1", "n=10", "n=100"]


def _family(name: str = "f.py::bench_fam") -> Entry:
    return Entry(name=name, fn=lambda s: None, case_labels=_LABELS)


def test_filter_pattern_is_regex_searched():
    r = Registry()
    for name in ("foo::bench_sort", "foo::bench_search", "foo::other"):
        r.add(Entry(name=name, fn=lambda s: None))
    # A plain word still matches as a substring.
    assert {e.name for e in r.filter("bench_(sort|search)")} == {
        "foo::bench_sort",
        "foo::bench_search",
    }
    assert [e.name for e in r.filter("other")] == ["foo::other"]
    assert [e.name for e in r.filter("sort$")] == ["foo::bench_sort"]
    # No pattern selects everything (in order); a non-matching one selects nothing.
    assert [e.name for e in r.filter(None)] == [
        "foo::bench_sort",
        "foo::bench_search",
        "foo::other",
    ]
    assert r.filter("nope") == []


def test_filter_invalid_regex_raises_value_error():
    r = Registry()
    r.add(Entry(name="a", fn=lambda s: None))
    with pytest.raises(ValueError, match="invalid benchmark filter pattern"):
        r.filter("foo(")


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
        (_LABELS, "n=999", []),
    ],
)
def test_narrow_entry(labels, pattern, cases):
    entry = Entry(name="f.py::bench_fam", fn=lambda s: None, case_labels=labels)
    narrowed = narrow_entry(entry, all_of=compile_name_filter(pattern))
    # No case left drops the entry; every case keeps it whole (`cases=None`).
    assert narrowed == (None if cases == [] else dataclasses.replace(entry, cases=cases))
    assert entry.cases is None  # a narrowed view is a copy


def test_narrow_and_or_compose():
    e = _family()
    # any_of (OR) picks cases 0 and 2; all_of (AND) keeps only 2.
    narrowed = narrow_entry(
        e,
        any_of=[compile_name_filter(r"n=1\]"), compile_name_filter(r"n=100\]")],
        all_of=compile_name_filter(r"n=100\]"),
    )
    assert narrowed is not None and narrowed.cases == [2]


def test_registry_filter_literal_selects_one_case_without_escaping():
    r = Registry()
    r.add(_family())
    (narrowed,) = r.filter("bench_fam[n=10]", literal=True)
    assert narrowed.cases == [1]
    # Without literal the bare brackets are a regex char class → no match.
    assert r.filter("bench_fam[n=10]") == []


def test_filter_tags_or_semantics():
    r = Registry()
    r.add(Entry(name="a", fn=lambda s: None, tags=frozenset({"io"})))
    r.add(Entry(name="b", fn=lambda s: None, tags=frozenset({"cpu"})))
    r.add(Entry(name="c", fn=lambda s: None, tags=frozenset({"io", "slow"})))
    r.add(Entry(name="d", fn=lambda s: None, tags=frozenset()))
    assert {e.name for e in r.filter(tags=["io"])} == {"a", "c"}
    assert {e.name for e in r.filter(tags=["io", "cpu"])} == {"a", "b", "c"}


def test_filter_pattern_and_tags_combine():
    r = Registry()
    r.add(Entry(name="foo::a", fn=lambda s: None, tags=frozenset({"io"})))
    r.add(Entry(name="bar::b", fn=lambda s: None, tags=frozenset({"io"})))
    r.add(Entry(name="foo::c", fn=lambda s: None, tags=frozenset({"cpu"})))
    result = r.filter("foo", tags=["io"])
    assert [e.name for e in result] == ["foo::a"]
