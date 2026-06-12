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


def test_run_registers_only_selected_cases_of_a_family():
    """A -k-narrowed family (entry.cases set) runs exactly those cases, with the
    right kwargs bound via the case index → state.range(0) → trampoline path."""
    seen_n = []

    @mew.parametrize([{"n": 1}, {"n": 10}, {"n": 100}], ids=["small", "mid", "big"])
    def bench_fam(state, n):
        seen_n.append(n)
        for _ in state:
            pass

    # Select small (case 0) and big (case 2) by label; mid is dropped.
    narrowed = mew.REGISTRY.filter(r"small|big")
    cap = Capture()
    mew.run(entries=narrowed, argv=_argv_fast(), reporter=cap)

    case_names = [r.benchmark_name() for r in cap.runs]
    assert all("bench_fam" in n for n in case_names)
    # Exactly cases 0 and 2 ran — mid (case 1) was dropped by the filter.
    assert sorted(n.split("/case:")[1] for n in case_names) == ["0", "2"]
    # The trampoline bound the kwargs for exactly cases 0 and 2 (n=1, n=100).
    assert sorted(seen_n) == [1, 100]


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


def test_state_pause_context_manager_excludes_work_from_timing():
    from mew._core import PauseScope

    seen: list[object] = []

    @mew.benchmark(iterations=1)
    def bench_x(state):
        for _ in state:
            cm = state.pause()
            assert isinstance(cm, PauseScope)
            with cm as entered:
                assert entered is cm

    @mew.benchmark(iterations=1)
    def bench_y(state):
        for _ in state:
            with state.pause():
                seen.append("inside")

    cap = Capture()
    mew.run(argv=["mew"], reporter=cap, filter=".*")
    assert cap.runs[0].iterations == 1
    assert cap.runs[1].iterations == 1
    assert seen == ["inside"]


def test_state_pause_excludes_paused_work_from_real_time():
    # Same workload in both benchmarks; one runs it inside `state.pause()`,
    # the other doesn't. The paused variant's measured real_time should be a
    # small fraction of the unpaused variant's.
    WORK = 200_000

    @mew.benchmark(iterations=1)
    def bench_unpaused(state):
        for _ in state:
            sum(range(WORK))

    @mew.benchmark(iterations=1)
    def bench_paused(state):
        for _ in state:
            with state.pause():
                sum(range(WORK))

    cap = Capture()
    mew.run(argv=["mew"], reporter=cap, filter=".*")
    unpaused, paused = cap.runs
    paused_time = paused.real_accumulated_time
    unpaused_time = unpaused.real_accumulated_time
    # Generous margin: paused real time should be at least 10x smaller than
    # the work it excluded. In practice it's typically 100x+ smaller.
    assert paused_time < unpaused_time / 10, f"paused={paused}s, unpaused={unpaused}s"


def test_state_pause_resumes_on_exception():
    @mew.benchmark(iterations=1)
    def bench_raises(state):
        for _ in state:
            try:
                with state.pause():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass

    cap = Capture()
    # Body completes normally because the exception is swallowed; ScopedPauseTiming's
    # destructor still resumes timing as the with-block unwinds.
    mew.run(argv=["mew"], reporter=cap, filter=".*")
    assert cap.runs[0].iterations == 1


def test_run_with_no_entries_returns_zero():
    cap = Capture()
    assert mew.run(argv=_argv_fast(), reporter=cap) == 0
    assert cap.runs == []


def test_state_batches_drives_body_in_multiples_of_n():
    body_calls: list[int] = []

    @mew.benchmark(iterations=10)
    def bench_batched(state):
        for n in state.batches(4):
            for _ in range(n):
                body_calls.append(1)

    cap = Capture()
    mew.run(argv=["mew"], reporter=cap, filter=".*")
    # 3 batches × 4 = 12 body calls; GB reports the actual count, not the cap.
    assert cap.runs[0].iterations == 12
    assert len(body_calls) == 12


def test_state_batches_rejects_non_positive_n():
    seen: list[type] = []

    @mew.benchmark(iterations=1)
    def bench_bad(state):
        try:
            state.batches(0)
        except ValueError as e:
            seen.append(type(e))
        for _ in state:
            pass

    cap = Capture()
    mew.run(argv=["mew"], reporter=cap, filter=".*")
    assert seen == [ValueError]
