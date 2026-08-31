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
    @mew.benchmark(min_time=0.5, unit="us", iterations=10)
    def bench_x(state):
        for _ in state:
            pass

    entry = REGISTRY.all()[0]
    assert entry.options["min_time"] == 0.5
    assert entry.options["unit"] == "us"
    assert entry.options["iterations"] == 10


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


def test_parametrize_registers_one_family_entry():
    @mew.parametrize([{"n": 1}, {"n": 10}, {"n": 100}])
    def bench_x(state, n):
        for _ in state:
            assert n in (1, 10, 100)

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["n=1", "n=10", "n=100"]


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


def test_parametrize_options_apply_to_all_variants():
    @mew.parametrize([{"n": 1}, {"n": 2}], min_time=0.25, unit="us")
    def bench_x(state, n):
        for _ in state:
            pass

    assert all(e.options["min_time"] == 0.25 for e in REGISTRY.all())
    assert all(e.options["unit"] == "us" for e in REGISTRY.all())


def test_parametrize_custom_ids():
    @mew.parametrize([{"n": 10}, {"n": 1000}], ids=["small", "big"])
    def bench_x(state, n):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["small", "big"]


def test_parametrize_ids_length_mismatch():
    with pytest.raises(ValueError, match="ids"):

        @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}], ids=["a", "b"])
        def _bench(state, n):
            for _ in state:
                pass


def test_parametrize_accepts_generator():
    @mew.parametrize({"n": n} for n in range(3))
    def bench_x(state, n):
        for _ in state:
            pass

    entries = REGISTRY.all()
    assert len(entries) == 1
    assert entries[0].case_labels == ["n=0", "n=1", "n=2"]


def test_trampoline_dispatches_by_state_range():
    seen = []
    labels_set = []

    @mew.parametrize([{"n": 7}, {"n": 9}])
    def bench_capture(state, n):
        seen.append(n)

    class DummyState:
        def __init__(self, idx):
            self._idx = idx

        def range(self, _pos):
            return self._idx

        def set_label(self, label):
            labels_set.append(label)

    (entry,) = REGISTRY.all()
    assert entry.case_labels is not None
    for i in range(len(entry.case_labels)):
        entry.fn(DummyState(i))  # ty: ignore[invalid-argument-type]
    assert seen == [7, 9]
    assert labels_set == ["n=7", "n=9"]


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


def test_threads_option_accepted_on_benchmark():
    @mew.benchmark(threads=4)
    def bench_x(state):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.options["threads"] == 4


def test_thread_range_option_accepted_on_parametrize():
    @mew.parametrize([{"n": 1}], thread_range=(1, 8))
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.options["thread_range"] == (1, 8)


def test_dense_thread_range_option_accepted_on_parametrize():
    @mew.parametrize([{"n": 1}], dense_thread_range=(1, 8, 1))
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.options["dense_thread_range"] == (1, 8, 1)


def test_product_pulls_threads_out_of_kwargs():
    @mew.product(n=[1, 2], threads=2)
    def bench_x(state, n):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1", "n=2"]  # threads is an option, not an axis
    assert entry.options["threads"] == 2


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
        "f/min_time:0.5",  # stripped by canonical_name on read
        "f/case:0",
        "f/threads:4",
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
        {"min_warmup_time": -0.1},
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


def test_double_registration_raises():
    with pytest.raises(RuntimeError, match="already registered"):

        @mew.benchmark
        @mew.parametrize([{"n": 1}])
        def _bench(state, n):
            for _ in state:
                pass

    # The failed outer decorator must not have added a second entry: the
    # registry still holds exactly the @parametrize registration.
    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["n=1"]


def test_parametrize_rejects_duplicate_case_labels():
    # Two list-valued cases both collapse to `data=list`, making `name[label]`
    # addressing ambiguous; registration must reject this, pointing at ids=.
    with pytest.raises(ValueError, match="duplicate case label"):

        @mew.parametrize([{"data": [1, 2]}, {"data": [3, 4]}])
        def _bench(state, data):
            for _ in state:
                pass

    assert REGISTRY.all() == []  # nothing half-registered


def test_parametrize_duplicate_labels_ok_with_explicit_ids():
    @mew.parametrize([{"data": [1, 2]}, {"data": [3, 4]}], ids=["small", "large"])
    def _bench(state, data):
        for _ in state:
            pass

    (entry,) = REGISTRY.all()
    assert entry.case_labels == ["small", "large"]


def test_threads_and_thread_range_mutually_exclusive():
    with pytest.raises(TypeError, match="mutually exclusive"):

        @mew.benchmark(threads=2, thread_range=(1, 4))
        def _bench(state):
            for _ in state:
                pass


def test_product_signature_covers_all_benchmark_options():
    # product() can't use **options: Unpack[BenchmarkOptions] like benchmark()/
    # parametrize() (its **kwargs slot is taken by **iterables), so each
    # BenchmarkOptions field must be listed by hand as a keyword-only param.
    # This guards against a field being added there but forgotten here.
    params = set(inspect.signature(mew.product).parameters)
    assert _OptionKeys <= params


def test_product_threads_and_thread_range_mutually_exclusive():
    with pytest.raises(TypeError, match="mutually exclusive"):

        @mew.product(threads=2, thread_range=(1, 4), n=[1, 2])
        def _bench(state, n):
            for _ in state:
                pass


def test_dense_thread_options_mutually_exclusive():
    with pytest.raises(TypeError, match="mutually exclusive"):

        @mew.benchmark(thread_range=(1, 4), dense_thread_range=(1, 4, 1))
        def _bench(state):
            for _ in state:
                pass


def test_thread_range_shape_validated_at_decoration():
    with pytest.raises(TypeError, match="min, max"):

        @mew.benchmark(thread_range=(1,))  # ty: ignore[invalid-argument-type]
        def _bench(state):
            for _ in state:
                pass

    with pytest.raises(TypeError, match="1 <= min <= max"):

        @mew.benchmark(thread_range=(4, 2))
        def _bench2(state):
            for _ in state:
                pass


def test_dense_thread_range_validated_at_decoration():
    with pytest.raises(TypeError, match="min, max, stride"):

        @mew.benchmark(dense_thread_range=(1, 4))  # ty: ignore[invalid-argument-type]
        def _bench(state):
            for _ in state:
                pass

    with pytest.raises(TypeError, match="stride >= 1"):

        @mew.benchmark(dense_thread_range=(1, 4, 0))
        def _bench2(state):
            for _ in state:
                pass


def test_benchmark_tags_propagate():
    @mew.benchmark(tags=("io", "slow"))
    def bench_x(state):
        for _ in state:
            pass

    entry = REGISTRY.all()[0]
    assert entry.tags == frozenset({"io", "slow"})


def test_benchmark_tags_accepts_single_string():
    @mew.benchmark(tags="io")
    def bench_x(state):
        for _ in state:
            pass

    assert REGISTRY.all()[0].tags == frozenset({"io"})


def test_parametrize_tags_apply_to_all_variants():
    @mew.parametrize([{"n": 1}, {"n": 2}], tags=("sort",))
    def bench_x(state, n):
        for _ in state:
            pass

    assert all(e.tags == frozenset({"sort"}) for e in REGISTRY.all())


def test_product_tags_apply_to_all_variants():
    @mew.product(n=[1, 2], algo=["a", "b"], tags=("sort", "heavy"))
    def bench_x(state, n, algo):
        for _ in state:
            pass

    assert all(e.tags == frozenset({"sort", "heavy"}) for e in REGISTRY.all())


def test_empty_tags_normalize_to_empty_frozenset():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    assert REGISTRY.all()[0].tags == frozenset()
