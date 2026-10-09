"""@benchmark, @parametrize, @product decorator behavior."""

from __future__ import annotations

import inspect

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


def test_unknown_option_raises():
    with pytest.raises(TypeError, match="unknown option"):

        @mew.benchmark(foo=1)
        def _bench(state):
            for _ in state:
                pass


def test_custom_name_override():
    @mew.benchmark(name="my/custom/name")
    def bench_x(state):
        for _ in state:
            pass

    assert REGISTRY.all()[0].name == "my/custom/name"


def test_parametrize_multi_kwarg_dict():
    @mew.parametrize(
        [
            {"n": 1, "algo": "a"},
            {"n": 10, "algo": "b"},
        ]
    )
    def bench_x(state, n, algo):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["n=1-algo=a", "n=10-algo=b"]


def test_parametrize_ids_length_mismatch():
    with pytest.raises(ValueError, match="ids"):

        @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}], ids=["a", "b"])
        def _bench(state, n):
            for _ in state:
                pass


def test_failed_parametrize_can_be_corrected():
    def bench_x(state, n):
        for _ in state:
            pass

    with pytest.raises(ValueError, match="duplicate case label"):
        mew.parametrize([{"n": []}, {"n": []}])(bench_x)

    mew.parametrize([{"n": []}, {"n": []}], ids=["first", "second"])(bench_x)
    assert REGISTRY.all()[0].case_labels == ["first", "second"]


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

    def bench_c(state, n):
        for _ in state:
            pass

    with pytest.raises(ValueError, match="already registered"):
        mew.parametrize([{"n": 1}], name="y")(bench_c)
    mew.parametrize([{"n": 1}], name="z")(bench_c)
    assert [e.name for e in REGISTRY.all()] == ["x", "y", "z"]


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
    assert entry.case_labels is not None
    assert set(entry.case_labels) == {
        "n=1-algo=a",
        "n=1-algo=b",
        "n=2-algo=a",
        "n=2-algo=b",
    }


def test_product_pulls_options_out_of_kwargs():
    @mew.product(n=[1, 2], min_time=0.05, unit="us")
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1", "n=2"]  # min_time/unit are options
    assert entry.options["min_time"] == 0.05
    assert entry.options["unit"] == "us"


def test_threads_accepts_a_sequence_of_counts():
    @mew.parametrize([{"n": 1}], threads=(n for n in [1, 2, 4, 4]))
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    # Snapshotted (the generator is consumed once) and de-duplicated in order.
    assert entry.options["threads"] == (1, 2, 4)


@pytest.mark.parametrize("threads", [0, True, [], [1, 0], [2.5], "4"])
def test_threads_validated_at_decoration(threads):
    with pytest.raises(TypeError, match="threads"):

        @mew.benchmark(threads=threads)
        def _bench(state):
            for _ in state:
                pass


def test_product_pulls_threads_out_of_kwargs():
    @mew.product(n=[1, 2], threads=2)
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1", "n=2"]  # threads is an option, not an axis
    assert entry.options["threads"] == (2,)


def test_product_needs_at_least_one_iterable():
    with pytest.raises(TypeError, match="at least one iterable"):

        @mew.product(min_time=0.1)
        def _bench(state):
            for _ in state:
                pass


@pytest.mark.parametrize(
    "bad",
    [
        "",
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


def test_parametrize_rejects_structurally_confusing_ids():
    with pytest.raises(ValueError, match="case label"):

        @mew.parametrize([{"n": 1}], ids=["a::b"])
        def _bench(state, n):
            for _ in state:
                pass

    assert REGISTRY.all() == []


def test_parametrize_rejects_structurally_confusing_derived_labels():
    # The label derives from the parameter value ("s=x[1]"); ids= is the
    # escape hatch for values whose repr collides with case addressing.
    with pytest.raises(ValueError, match="case label"):

        @mew.parametrize([{"s": "x[1]"}])
        def _bench(state, s):
            for _ in state:
                pass

    assert REGISTRY.all() == []


@pytest.mark.parametrize(
    "options",
    [
        {"iterations": 0},
        {"repetitions": -1},
        {"threads": 0},
        {"min_time": 0.0},
        {"min_time": float("nan")},
        {"min_warmup_time": -0.1},
        {"min_warmup_time": float("nan")},
    ],
)
def test_decorators_reject_out_of_range_options(options):
    # Google Benchmark guards these with asserts compiled out of release
    # builds, so mew validates at decoration time.
    with pytest.raises(TypeError, match="must be"):

        @mew.benchmark(**options)
        def _bench(state):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


@pytest.mark.parametrize(
    "options",
    [{"iterations": 5, "min_time": 0.1}, {"iterations": 5, "min_warmup_time": 0.1}],
)
def test_decorators_reject_iterations_with_a_time_budget(options):
    # GB checks this combination only in debug builds and otherwise ignores
    # the time budget.
    with pytest.raises(TypeError, match="iterations cannot be combined with min_"):

        @mew.benchmark(**options)
        def _bench(state):
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


def test_product_signature_covers_all_benchmark_options():
    # product()'s **kwargs are the iterables, so each BenchmarkOptions field is
    # listed by hand; catch a new field that was forgotten there.
    params = set(inspect.signature(mew.product).parameters)
    assert _OptionKeys <= params


@pytest.mark.parametrize(
    ("decorate", "tags", "expected"),
    [
        (mew.benchmark, ("io", "slow"), frozenset({"io", "slow"})),
        (mew.benchmark, "io", frozenset({"io"})),
        (mew.benchmark, None, frozenset()),
        (
            lambda **kw: mew.parametrize([{"n": 1}, {"n": 2}], **kw),
            ("sort",),
            frozenset({"sort"}),
        ),
        (
            lambda **kw: mew.product(n=[1, 2], algo=["a", "b"], **kw),
            ("sort", "heavy"),
            frozenset({"sort", "heavy"}),
        ),
    ],
)
def test_tags_normalize_to_a_frozenset_on_every_decorator(decorate, tags, expected):
    @decorate(**({} if tags is None else {"tags": tags}))
    def bench_x(state, **_):
        for _ in state:
            pass

    assert all(e.tags == expected for e in REGISTRY.all())
