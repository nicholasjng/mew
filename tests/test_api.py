"""@benchmark and @parametrize decorator behavior, and grid()."""

from __future__ import annotations

import pytest

import mew
from mew._registry import REGISTRY


def test_bare_decorator_registers_one_entry():
    """A bare `@mew.benchmark` registers one entry with no options."""

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    # Relative to the project root, as `mew compare` keys it.
    assert entries[0].name == (
        "tests/test_api.py::test_bare_decorator_registers_one_entry.<locals>.bench_x"
    )
    assert entries[0].fn is bench_x
    assert entries[0].options == {}


def test_called_decorator_captures_options():
    """`@mew.benchmark(...)` and `@mew.parametrize(...)` store their options on the entry."""

    @mew.benchmark(min_time=0.5, use_real_time=True, repetitions=10)
    def bench_x(state):
        for _ in state:
            pass

    # A parametrized family is one entry; its options apply to every case.
    @mew.parametrize([{"n": 1}, {"n": 2}], min_time=0.25, use_real_time=True)
    def bench_family(state, n):
        for _ in state:
            pass

    entry, family = REGISTRY.all()
    assert entry.options["min_time"] == 0.5
    assert entry.options["use_real_time"] is True
    assert entry.options["repetitions"] == 10
    assert family.case_labels == ["n=1", "n=2"]
    assert family.options["min_time"] == 0.25
    assert family.options["use_real_time"] is True


def test_min_time_takes_run_syntax_and_nx_fixes_the_iteration_count():
    """Decorators take `min_time` in run syntax; `Nx` becomes a fixed `iterations` count."""

    @mew.benchmark(min_time="3x")
    def bench_a(state):
        pass

    @mew.parametrize([{"n": 1}], min_time="0.25s")
    def bench_b(state, n):
        pass

    # Google Benchmark has no per-benchmark "Nx"; its equivalent is iterations.
    @mew.parametrize(mew.grid(n=[1]), min_time="7x")
    def bench_c(state, n):
        pass

    @mew.benchmark(min_time="250ms")
    def bench_d(state):
        pass

    assert [e.options for e in REGISTRY.all()] == [
        {"iterations": 3},
        {"min_time": 0.25},
        {"iterations": 7},
        {"min_time": 0.25},
    ]


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"foo": 1}, TypeError, "unknown option"),
        # A bool is an int, but not a count.
        ({"repetitions": True}, TypeError, "repetitions must be an integer"),
        ({"iterations": 2.5}, TypeError, "iterations must be an integer"),
        ({"iterations": 0}, ValueError, "iterations must be >= 1"),
        ({"repetitions": -1}, ValueError, "repetitions must be >= 1"),
        ({"threads": True}, TypeError, "threads must be an int or"),
        ({"threads": [2.5]}, TypeError, "threads must be an integer"),
        ({"threads": "4"}, TypeError, "threads must be an integer"),
        ({"threads": 0}, ValueError, "threads must be >= 1"),
        ({"threads": []}, ValueError, "at least one thread count"),
        ({"min_time": True}, TypeError, "min_time: expected a duration"),
        ({"min_time": 0.0}, ValueError, "min_time: expected a positive duration"),
        ({"min_time": float("nan")}, ValueError, "min_time: expected a positive duration"),
        ({"min_time": "1.5x"}, ValueError, "min_time: expected a positive duration"),
        ({"min_warmup_time": "0.1"}, TypeError, "min_warmup_time must be a number"),
        ({"min_warmup_time": -0.1}, ValueError, "min_warmup_time must be a finite"),
        ({"min_warmup_time": float("nan")}, ValueError, "min_warmup_time must be a finite"),
        # Times are always nanoseconds, so there is no unit to pick.
        ({"unit": "us"}, TypeError, "unknown option"),
        ({"use_real_time": 1}, TypeError, "use_real_time must be a bool"),
        # Google Benchmark checks these only in debug builds, which abort.
        (
            {"iterations": 5, "min_time": 0.1},
            ValueError,
            "iterations cannot be combined with min_time",
        ),
        (
            {"iterations": 5, "min_warmup_time": 0.1},
            ValueError,
            "iterations cannot be combined with min_warmup_time",
        ),
        (
            {"min_time": "5x", "iterations": 5},
            ValueError,
            "min_time='5x' cannot be combined with iterations",
        ),
        (
            {"min_time": "5x", "min_warmup_time": 0.1},
            ValueError,
            "min_time='5x' cannot be combined with min_warmup_time",
        ),
        (
            {"use_real_time": True, "use_manual_time": True},
            ValueError,
            "use_real_time cannot be combined with use_manual_time",
        ),
    ],
)
def test_decorators_reject_invalid_options(options, error, message):
    """`@mew.benchmark` rejects invalid options and conflicting combinations without registering."""
    with pytest.raises(error, match=message):

        @mew.benchmark(**options)
        def _bench(state):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_parametrize_ids_length_mismatch():
    """`@mew.parametrize` raises when `ids` and the cases differ in length."""
    with pytest.raises(ValueError, match="ids"):

        @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}], ids=["a", "b"])
        def _bench(state, n):
            for _ in state:
                pass


def test_parametrize_accepts_generator():
    """`@mew.parametrize` accepts a generator of cases."""

    @mew.parametrize({"n": n} for n in range(3))
    def bench_x(state, n):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["n=0", "n=1", "n=2"]


def test_grid_is_the_cartesian_product():
    """`grid` gives every combination, the last axis varying fastest; any name is an axis,
    including those of benchmark options, which `parametrize` takes separately."""
    assert mew.grid(n=[1, 2], threads=["a", "b"]) == [
        {"n": 1, "threads": "a"},
        {"n": 1, "threads": "b"},
        {"n": 2, "threads": "a"},
        {"n": 2, "threads": "b"},
    ]

    @mew.parametrize(mew.grid(n=[1, 2], unit=(u for u in ["kb", "mb"])), min_time="1x")
    def bench_x(state, n, unit):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1-unit=kb", "n=1-unit=mb", "n=2-unit=kb", "n=2-unit=mb"]
    assert entry.options == {"iterations": 1}  # options stay options


def test_parametrize_needs_a_case():
    """An empty family, such as a filtered grid that kept nothing, is an error, not a family
    that silently runs nothing."""
    with pytest.raises(ValueError, match="parametrize needs at least one case"):

        @mew.parametrize([c for c in mew.grid(n=[1, 2]) if c["n"] > 2])
        def _bench(state, n):
            for _ in state:
                pass


@pytest.mark.parametrize(
    ("axes", "message"),
    [
        ({}, "at least one axis"),
        ({"algo": "merge"}, "grid axis 'algo' must be an iterable"),  # would split into letters
        ({"n": 10}, "grid axis 'n' must be an iterable"),
    ],
)
def test_grid_rejects_a_missing_or_non_iterable_axis(axes, message):
    """`grid` needs an axis, and each axis is a collection of values, not one value."""
    with pytest.raises(TypeError, match=message):
        mew.grid(**axes)


def test_threads_accepts_a_sequence_of_counts():
    """`threads` accepts an iterable of counts, stored as a tuple with duplicates removed."""

    @mew.parametrize([{"n": 1}], threads=(n for n in [1, 2, 4, 4]))
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    # Snapshotted (the generator is consumed once) and de-duplicated in order.
    assert entry.options["threads"] == (1, 2, 4)


@pytest.mark.parametrize(
    "bad",
    [
        "   ",
        "a::b",  # stdin selector / file-prefix separator
        "a[b",  # case-addressing brackets
        "a]b",
        "a\nb",  # line-oriented list/stdin output
    ],
)
def test_benchmark_rejects_structurally_confusing_names(bad):
    """Benchmark names that are blank or contain `::`, `[`, `]` or a newline are rejected."""
    with pytest.raises(ValueError, match="benchmark name"):

        @mew.benchmark(name=bad)
        def _bench(state):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_benchmark_allows_slash_hierarchy_names():
    """Benchmark names may use `/` to form a hierarchy."""

    # `/` grouping (the Google Benchmark convention) stays legal.
    @mew.benchmark(name="suite/sort/insertion")
    def _bench(state):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.name == "suite/sort/insertion"


# A label derived from a value ("s=x[1]") is checked too; ids= is the escape
# hatch for values that collide with case addressing.
@pytest.mark.parametrize("ids", [["a::b"], None])
def test_parametrize_rejects_structurally_confusing_case_labels(ids):
    """`@mew.parametrize` rejects case labels with `::` or brackets, from `ids` or from values."""
    with pytest.raises(ValueError, match="case label"):

        @mew.parametrize([{"s": "x[1]"}], ids=ids)
        def _bench(state, s):
            for _ in state:
                pass

    assert REGISTRY.all() == []


def test_double_registration_raises():
    """`@mew.benchmark` over `@mew.parametrize` registers the same name, so it raises and keeps
    only the first entry."""
    with pytest.raises(ValueError, match="already registered"):

        @mew.benchmark
        @mew.parametrize([{"n": 1}])
        def _bench(state, n):
            for _ in state:
                pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1"]


def test_parametrize_rejects_duplicate_case_labels():
    """`@mew.parametrize` rejects cases whose labels are the same."""
    # Both cases collapse to `data=list`, making `name[label]` addressing ambiguous.
    with pytest.raises(ValueError, match="duplicate case label"):

        @mew.parametrize([{"data": [1, 2]}, {"data": [3, 4]}])
        def _bench(state, data):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_tags_normalize_to_a_frozenset_on_every_decorator():
    """Every decorator stores `tags` as a frozenset; a string is one tag, no tags is empty."""

    @mew.benchmark(tags=("io", "slow"))
    def bench_a(state):
        pass

    @mew.benchmark(tags="io")  # a string is one tag
    def bench_b(state):
        pass

    @mew.benchmark
    def bench_c(state):
        pass

    @mew.parametrize([{"n": 1}, {"n": 2}], tags=["sort"])
    def bench_d(state, n):
        pass

    @mew.parametrize(mew.grid(n=[1, 2]), tags="sort")
    def bench_e(state, n):
        pass

    assert [e.tags for e in REGISTRY.all()] == [
        frozenset({"io", "slow"}),
        frozenset({"io"}),
        frozenset(),
        frozenset({"sort"}),
        frozenset({"sort"}),
    ]


def test_benchmark_given_a_positional_name_says_options_are_keywords():
    """`@mew.benchmark("name")` raises `TypeError` with a hint, not an `AttributeError`."""
    with pytest.raises(TypeError, match="takes its options as keywords"):
        mew.benchmark("my_name")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("ids", [[1, 2], "ab"])
def test_parametrize_ids_must_be_strings(ids):
    """Non-string ids raise `TypeError`; a string would split into one-character labels."""
    with pytest.raises(TypeError, match="ids must be a sequence of strings"):

        @mew.parametrize([{"n": 1}, {"n": 2}], ids=ids)
        def _bench(state, n):
            for _ in state:
                pass


def test_a_benchmark_without_a_module_or_file_is_named_unknown():
    """Code from `exec` with empty globals has no source file and a `None` module."""
    code = "import mew\n\n@mew.benchmark\ndef bench_x(state):\n    for _ in state:\n        pass\n"
    exec(compile(code, "<string>", "exec"), {})  # ruff: ignore[S102]
    assert [e.name for e in REGISTRY.all()] == ["<unknown>::bench_x"]
