"""End-to-end: register via Python API, run via the C++ runner, capture results."""

from __future__ import annotations

from typing import Any

import mew


class Capture:
    """Minimal Reporter that just stashes everything for assertion."""

    def __init__(self) -> None:
        self.context: dict[str, Any] | None = None
        self.runs: list[Any] = []
        self.finalized = False

    def report_context(self, context):
        self.context = context
        return True

    def report_runs(self, runs):
        self.runs.extend(runs)

    def finalize(self):
        self.finalized = True


def _argv_fast() -> list[str]:
    # `--benchmark_min_time=1x` forces exactly one iteration per benchmark,
    # which keeps the test suite fast and deterministic.
    return ["mew", "--benchmark_min_time=1x"]


def test_run_single_benchmark_captures_one_run():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    n = mew.run(argv=_argv_fast(), reporter=cap)
    assert n == 1
    assert len(cap.runs) == 1
    assert cap.finalized
    assert cap.context is not None
    assert cap.context["num_cpus"] >= 1


def test_run_parametrize_emits_one_run_per_variant():
    @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}])
    def bench_x(state, n):
        for _ in state:
            pass

    cap = Capture()
    mew.run(argv=_argv_fast(), reporter=cap)
    names = [r.benchmark_name() for r in cap.runs]
    assert len(names) == 3


def test_run_multiple_reporters_fan_out():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    a = Capture()
    b = Capture()
    mew.run(argv=_argv_fast(), reporter=[a, b])
    assert len(a.runs) == 1
    assert len(b.runs) == 1
    assert a.finalized and b.finalized


def test_run_options_iterations_applied():
    @mew.benchmark(iterations=42)
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    # When iterations is set on the handle, GB ignores --benchmark_min_time.
    mew.run(argv=["mew"], reporter=cap)
    assert cap.runs[0].iterations == 42


def test_run_filter_selects_subset():
    @mew.benchmark
    def bench_a(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_b(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(argv=_argv_fast(), reporter=cap, filter="bench_a")
    names = [r.benchmark_name() for r in cap.runs]
    assert all("bench_a" in n for n in names)
    assert not any("bench_b" in n for n in names)


def test_run_with_no_entries_returns_zero():
    cap = Capture()
    assert mew.run(argv=_argv_fast(), reporter=cap) == 0
    assert cap.runs == []
