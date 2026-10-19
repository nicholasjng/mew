"""@benchmark, @parametrize, @product decorator behavior."""

from __future__ import annotations

from typing import Any

import pytest

import mew
from mew._registry import REGISTRY
from mew.api import _OptionKeys


def test_bare_decorator_registers_one_entry():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert "bench_x" in entries[0].name
    assert entries[0].fn is bench_x
    assert entries[0].options == {}


def test_called_decorator_captures_options():
    @mew.benchmark(min_time=0.5, unit="us", repetitions=10)
    def bench_x(state):
        for _ in state:
            pass

    # A parametrized family is one entry; its options apply to every case.
    @mew.parametrize([{"n": 1}, {"n": 2}], min_time=0.25, unit="us")
    def bench_family(state, n):
        for _ in state:
            pass

    entry, family = REGISTRY.all()
    assert entry.options["min_time"] == 0.5
    assert entry.options["unit"] == "us"
    assert entry.options["repetitions"] == 10
    assert family.case_labels == ["n=1", "n=2"]
    assert family.options["min_time"] == 0.25
    assert family.options["unit"] == "us"


def test_min_time_takes_run_syntax_and_nx_fixes_the_iteration_count():
    @mew.benchmark(min_time="3x")
    def bench_a(state):
        pass

    @mew.parametrize([{"n": 1}], min_time="0.25s")
    def bench_b(state, n):
        pass

    # Google Benchmark has no per-benchmark "Nx"; its equivalent is iterations.
    @mew.product(n=[1], min_time=" 7x ")
    def bench_c(state, n):
        pass

    assert [e.options for e in REGISTRY.all()] == [
        {"iterations": 3},
        {"min_time": 0.25},
        {"iterations": 7},
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
        ({"min_time": True}, TypeError, "min_time must be seconds"),
        ({"min_time": 0.0}, ValueError, "min_time must be positive"),
        ({"min_time": float("nan")}, ValueError, "min_time must be positive"),
        ({"min_time": "1.5x"}, ValueError, "min_time must be positive"),
        ({"min_warmup_time": "0.1"}, TypeError, "min_warmup_time must be a number"),
        ({"min_warmup_time": -0.1}, ValueError, "min_warmup_time must be a finite"),
        ({"min_warmup_time": float("nan")}, ValueError, "min_warmup_time must be a finite"),
        ({"min_warmup_time": 0.1}, ValueError, "min_warmup_time needs min_time"),
        # Unchecked, a misspelled unit fails only once the benchmark runs.
        ({"unit": 1}, TypeError, "unit must be a string"),
        ({"unit": "usec"}, ValueError, "unit must be one of 'ns', 'us', 'ms', 's'"),
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
    with pytest.raises(error, match=message):

        @mew.benchmark(**options)
        def _bench(state):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_parametrize_ids_length_mismatch():
    with pytest.raises(ValueError, match="ids"):

        @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}], ids=["a", "b"])
        def _bench(state, n):
            for _ in state:
                pass


def test_rejected_name_can_be_corrected():
    def bench_a(state):
        for _ in state:
            pass

    def bench_b(state):
        for _ in state:
            pass

    mew.benchmark(name="x")(bench_a)
    with pytest.raises(ValueError, match="already registered"):
        mew.benchmark(name="x")(bench_b)
    mew.benchmark(name="y")(bench_b)
    assert [e.name for e in REGISTRY.all()] == ["x", "y"]


def test_parametrize_accepts_generator():
    @mew.parametrize({"n": n} for n in range(3))
    def bench_x(state, n):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["n=0", "n=1", "n=2"]


def test_product_cartesian():
    @mew.product(n=[1, 2], algo=["a", "b"])
    def bench_x(state, n, algo):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    # Cases follow itertools.product: the last axis varies fastest.
    assert entry.case_labels == ["n=1-algo=a", "n=1-algo=b", "n=2-algo=a", "n=2-algo=b"]


# Each variant drops the options that contradict the other's, so between them
# every option is forwarded.
@pytest.mark.parametrize(
    "dropped", [{"iterations", "use_manual_time"}, {"min_time", "min_warmup_time", "use_real_time"}]
)
def test_product_passes_every_option_through(dropped):
    # product()'s **kwargs are the iterables, so it forwards each option by
    # hand; a forgotten one would be silently dropped.
    every: dict[str, Any] = {
        "min_time": 0.05,
        "min_warmup_time": 0.01,
        "iterations": 10,
        "repetitions": 3,
        "unit": "us",
        "use_real_time": True,
        "use_manual_time": True,
        "measure_process_cpu_time": True,
        "threads": 2,
    }
    assert set(every) == _OptionKeys  # a new option needs a value here
    options = {k: v for k, v in every.items() if k not in dropped}

    @mew.product(n=[1, 2], **options)
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1", "n=2"]  # options, not axes
    assert entry.options == {**options, "threads": (2,)}


def test_threads_accepts_a_sequence_of_counts():
    @mew.parametrize([{"n": 1}], threads=(n for n in [1, 2, 4, 4]))
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    # Snapshotted (the generator is consumed once) and de-duplicated in order.
    assert entry.options["threads"] == (1, 2, 4)


def test_product_needs_at_least_one_iterable():
    with pytest.raises(TypeError, match="at least one iterable"):

        @mew.product(min_time=0.1)
        def _bench(state):
            for _ in state:
                pass


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
    with pytest.raises(ValueError, match="benchmark name"):

        @mew.benchmark(name=bad)
        def _bench(state):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_benchmark_allows_slash_hierarchy_names():
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
    with pytest.raises(ValueError, match="case label"):

        @mew.parametrize([{"s": "x[1]"}], ids=ids)
        def _bench(state, s):
            for _ in state:
                pass

    assert REGISTRY.all() == []


def test_double_registration_raises():
    with pytest.raises(RuntimeError, match="already registered"):

        @mew.benchmark
        @mew.parametrize([{"n": 1}])
        def _bench(state, n):
            for _ in state:
                pass

    # The failed outer decorator must not add a second entry.
    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1"]


def test_parametrize_rejects_duplicate_case_labels():
    # Both cases collapse to `data=list`, making `name[label]` addressing ambiguous.
    with pytest.raises(ValueError, match="duplicate case label"):

        @mew.parametrize([{"data": [1, 2]}, {"data": [3, 4]}])
        def _bench(state, data):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_tags_normalize_to_a_frozenset_on_every_decorator():
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

    @mew.product(n=[1, 2], tags="sort")
    def bench_e(state, n):
        pass

    assert [e.tags for e in REGISTRY.all()] == [
        frozenset({"io", "slow"}),
        frozenset({"io"}),
        frozenset(),
        frozenset({"sort"}),
        frozenset({"sort"}),
    ]
