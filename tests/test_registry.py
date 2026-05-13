"""Registry behavior."""

from __future__ import annotations

from mew._registry import REGISTRY, Entry, Registry


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


def test_filter_tags_or_semantics():
    r = Registry()
    r.add(Entry(name="a", fn=lambda s: None, tags=("io",)))
    r.add(Entry(name="b", fn=lambda s: None, tags=("cpu",)))
    r.add(Entry(name="c", fn=lambda s: None, tags=("io", "slow")))
    r.add(Entry(name="d", fn=lambda s: None, tags=()))
    # OR across requested tags
    assert {e.name for e in r.filter(tags=["io"])} == {"a", "c"}
    assert {e.name for e in r.filter(tags=["io", "cpu"])} == {"a", "b", "c"}
    # Entries with no tags are excluded when a tag filter is active
    assert "d" not in {e.name for e in r.filter(tags=["io"])}


def test_filter_pattern_and_tags_combine():
    r = Registry()
    r.add(Entry(name="foo::a", fn=lambda s: None, tags=("io",)))
    r.add(Entry(name="bar::b", fn=lambda s: None, tags=("io",)))
    r.add(Entry(name="foo::c", fn=lambda s: None, tags=("cpu",)))
    result = r.filter("foo", tags=["io"])
    assert [e.name for e in result] == ["foo::a"]


def test_module_global_registry_is_singleton():
    REGISTRY.add(Entry(name="x", fn=lambda s: None))
    assert any(e.name == "x" for e in REGISTRY.all())
