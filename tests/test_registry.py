"""Registry behavior."""

from __future__ import annotations

import pytest

from mew._registry import REGISTRY, Entry, Registry, compile_name_filter, narrow_entry


def _family(name: str = "f.py::bench_fam") -> Entry:
    return Entry(name=name, fn=lambda s: None, case_labels=["n=1", "n=10", "n=100"])


def _narrow(entry: Entry, pattern: str) -> Entry | None:
    return narrow_entry(entry, all_of=compile_name_filter(pattern))


def test_add_and_clear():
    r = Registry()
    r.add(Entry(name="a", fn=lambda s: None))
    r.add(Entry(name="b", fn=lambda s: None))
    assert len(r) == 2
    r.clear()
    assert len(r) == 0


def test_filter_substring():
    r = Registry()
    r.add(Entry(name="foo::a", fn=lambda s: None))
    r.add(Entry(name="foo::b", fn=lambda s: None))
    r.add(Entry(name="bar::c", fn=lambda s: None))
    assert [e.name for e in r.filter("foo")] == ["foo::a", "foo::b"]
    assert [e.name for e in r.filter(None)] == ["foo::a", "foo::b", "bar::c"]
    assert r.filter("nope") == []


def test_filter_pattern_is_regex_searched():
    r = Registry()
    for name in ("foo::bench_sort", "foo::bench_search", "foo::other"):
        r.add(Entry(name=name, fn=lambda s: None))
    # Alternation matches both bench_* entries; a plain word still works (substring).
    assert {e.name for e in r.filter("bench_(sort|search)")} == {
        "foo::bench_sort",
        "foo::bench_search",
    }
    assert [e.name for e in r.filter("other")] == ["foo::other"]
    # Anchors honored: `$` pins the end.
    assert [e.name for e in r.filter("sort$")] == ["foo::bench_sort"]


def test_filter_invalid_regex_raises_value_error():
    r = Registry()
    r.add(Entry(name="a", fn=lambda s: None))
    with pytest.raises(ValueError, match="invalid benchmark filter pattern"):
        r.filter("foo(")


def test_narrow_plain_benchmark_is_all_or_nothing():
    e = Entry(name="f.py::bench_x", fn=lambda s: None)
    assert _narrow(e, "bench_x") is e  # unchanged view, no cases
    assert _narrow(e, "nope") is None


def test_narrow_family_name_match_keeps_all_cases():
    e = _family()
    narrowed = _narrow(e, "bench_fam")
    assert narrowed is e  # whole family → no narrowing
    assert narrowed.cases is None


def test_narrow_by_human_label_selects_one_case():
    # `name[label]` is addressable; only case index 2 (n=100) matches.
    narrowed = _narrow(_family(), r"bench_fam\[n=100\]")
    assert narrowed is not None
    assert narrowed.cases == [2]


def test_narrow_by_case_index_selects_one_case():
    narrowed = _narrow(_family(), "case:1")
    assert narrowed is not None and narrowed.cases == [1]


def test_narrow_alternation_selects_multiple_cases():
    narrowed = _narrow(_family(), r"n=1\]|n=100\]")  # labels n=1 and n=100, not n=10
    assert narrowed is not None and narrowed.cases == [0, 2]


def test_narrow_no_case_matches_drops_family():
    assert _narrow(_family(), "n=999") is None


def test_narrow_all_cases_match_collapses_to_whole_family():
    # A pattern matching every case needn't narrow — keep the dense path.
    e = _family()
    narrowed = _narrow(e, r"case:\d")
    assert narrowed is e  # same object, no replace
    assert narrowed.cases is None


def test_narrow_and_or_compose():
    e = _family()
    # any_of (OR) picks cases 0 and 2; all_of (AND) keeps only 2.
    narrowed = narrow_entry(
        e,
        any_of=[compile_name_filter(r"n=1\]"), compile_name_filter(r"n=100\]")],
        all_of=compile_name_filter("case:2"),
    )
    assert narrowed is not None and narrowed.cases == [2]


def test_registry_filter_narrows_family():
    r = Registry()
    r.add(_family())
    (narrowed,) = r.filter(r"bench_fam\[n=10\]")
    assert narrowed.cases == [1]


def test_compile_name_filter_literal_escapes_brackets():
    # As a regex, `[n=10]` is a char class and won't match the literal label;
    # literal=True escapes it so the displayed name[label] matches as-is.
    name = "f.py::bench_fam[n=10]"
    assert compile_name_filter("bench_fam[n=10]").search(name) is None
    assert compile_name_filter("bench_fam[n=10]", literal=True).search(name) is not None


def test_registry_filter_literal_selects_one_case_without_escaping():
    # The win: an unescaped, pasted `name[label]` selects exactly that case.
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
    # OR across requested tags
    assert {e.name for e in r.filter(tags=["io"])} == {"a", "c"}
    assert {e.name for e in r.filter(tags=["io", "cpu"])} == {"a", "b", "c"}
    # Entries with no tags are excluded when a tag filter is active
    assert "d" not in {e.name for e in r.filter(tags=["io"])}


def test_filter_pattern_and_tags_combine():
    r = Registry()
    r.add(Entry(name="foo::a", fn=lambda s: None, tags=frozenset({"io"})))
    r.add(Entry(name="bar::b", fn=lambda s: None, tags=frozenset({"io"})))
    r.add(Entry(name="foo::c", fn=lambda s: None, tags=frozenset({"cpu"})))
    result = r.filter("foo", tags=["io"])
    assert [e.name for e in result] == ["foo::a"]


def test_module_global_registry_is_singleton():
    REGISTRY.add(Entry(name="x", fn=lambda s: None))
    assert any(e.name == "x" for e in REGISTRY.all())
