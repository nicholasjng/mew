"""@benchmark, @parametrize, @product decorator behavior."""

from __future__ import annotations

import pytest

import mew
from mew._registry import REGISTRY

# ---------- @benchmark ------------------------------------------------------


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


# ---------- @parametrize ----------------------------------------------------


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


# ---------- @product --------------------------------------------------------


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


def test_product_needs_at_least_one_iterable():
    with pytest.raises(TypeError, match="at least one iterable"):

        @mew.product(min_time=0.1)
        def _bench(state):
            for _ in state:
                pass


# ---------- composition guard ----------------------------------------------


def test_double_registration_raises():
    with pytest.raises(RuntimeError, match="already registered"):

        @mew.benchmark
        @mew.parametrize([{"n": 1}])
        def _bench(state, n):
            for _ in state:
                pass


# ---------- tags -----------------------------------------------------------


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
