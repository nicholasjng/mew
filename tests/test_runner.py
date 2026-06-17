"""End-to-end: register via Python API, run via the C++ runner, capture results."""

from __future__ import annotations

import sys
from typing import Any

import pytest

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


def test_run_single_benchmark_captures_one_run():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    n = mew.run(min_time="1x", reporter=cap)
    assert n == 1
    assert len(cap.runs) == 1
    assert cap.finalized
    assert cap.context is not None
    assert cap.context["num_cpus"] >= 1


def test_run_benchmarks_extra_context_overlays_onto_report_context():
    """The binding merges `extra_context` into the GB context, last-wins.

    Exercises the `run_benchmarks(..., extra_context=...)` parameter directly:
    overlaid keys reach `report_context`, override GB-provided keys, and the
    untouched GB keys still come through.
    """
    from mew import _core

    def bench(state):
        for _ in state:
            pass

    _core.clear_registered_benchmarks()
    _core.register_benchmark("bench_overlay", bench)
    try:
        cap = Capture()
        extra = {"session_id": "sid-123", "host_name": "overridden", "custom": {"k": "v"}}
        _core.run_benchmarks(["mew", "--benchmark_min_time=1x"], cap, extra)
    finally:
        _core.clear_registered_benchmarks()

    assert cap.context is not None
    assert cap.context["session_id"] == "sid-123"
    assert cap.context["custom"] == {"k": "v"}
    # Overlay wins over the value GB put in the context...
    assert cap.context["host_name"] == "overridden"
    # ...while GB keys absent from extra_context pass through untouched.
    assert cap.context["num_cpus"] >= 1


def test_run_parametrize_emits_one_run_per_variant():
    @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}])
    def bench_x(state, n):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    names = [r["name"] for r in cap.runs]
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
    mew.run(entries=narrowed, min_time="1x", reporter=cap)

    case_names = [r["name"] for r in cap.runs]
    assert all("bench_fam" in n for n in case_names)
    # Exactly cases 0 and 2 ran — mid (case 1) was dropped by the filter.
    assert sorted(n.split("/case:")[1] for n in case_names) == ["0", "2"]
    # The trampoline bound the kwargs for exactly cases 0 and 2 (n=1, n=100).
    assert sorted(seen_n) == [1, 100]


def test_is_threaded_helper():
    from mew.runner import _is_threaded

    assert not _is_threaded({})
    assert not _is_threaded({"threads": 1})
    assert _is_threaded({"threads": 2})
    assert not _is_threaded({"thread_range": (1, 1)})
    assert _is_threaded({"thread_range": (1, 8)})


def test_threaded_benchmark_skipped_on_gil_build(monkeypatch):
    """On a GIL interpreter, threaded mode would deadlock on GB's start barrier.
    By default mew warns and emits a skipped row rather than running (or hanging)."""
    from mew import runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: True)

    @mew.benchmark(threads=4)
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    with pytest.warns(RuntimeWarning, match="skipping 1 threaded benchmark"):
        n = mew.run(min_time="1x", reporter=cap)

    assert n == 0  # nothing actually executed by GB
    assert len(cap.runs) == 1
    row = cap.runs[0]
    assert row["skipped"] is True
    assert row["threads"] == 4
    assert "free-threaded" in row["skip_message"]
    assert cap.finalized


def test_threaded_benchmark_strict_raises_on_gil_build(monkeypatch):
    """`strict=True` restores the hard error for CI where the skip would mask a
    misconfiguration."""
    from mew import runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: True)

    @mew.benchmark(threads=4)
    def bench_x(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="free-threaded interpreter"):
        mew.run(min_time="1x", reporter=Capture(), strict=True)


def test_mixed_suite_skips_threaded_runs_rest_on_gil_build(monkeypatch):
    """A mixed suite runs its non-threaded benchmarks and skips only the threaded
    ones — the dual-interpreter workflow."""
    from mew import runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: True)

    @mew.benchmark(threads=4)
    def bench_threaded(state):
        for _ in state:
            pass

    @mew.benchmark
    def bench_plain(state):
        for _ in state:
            pass

    cap = Capture()
    with pytest.warns(RuntimeWarning):
        n = mew.run(min_time="1x", reporter=cap)

    assert n == 1  # bench_plain ran
    threaded_rows = [r for r in cap.runs if "bench_threaded" in r["name"]]
    plain_rows = [r for r in cap.runs if "bench_plain" in r["name"]]
    assert threaded_rows and all(r["skipped"] for r in threaded_rows)
    assert plain_rows and not any(r["skipped"] for r in plain_rows)


def test_threaded_benchmark_warms_up_on_free_threaded(monkeypatch):
    """On a free-threaded build the guard passes and mew warms the threading
    state before invoking the C++ runner (which we stub to avoid real threads)."""
    from mew import _core, runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: False)
    monkeypatch.setattr(runner, "_FT_WARMED_UP", False)
    warmed = []
    monkeypatch.setattr(runner, "_warmup_free_threading", lambda: warmed.append(True))
    monkeypatch.setattr(_core, "run_benchmarks", lambda *a, **k: 1)

    @mew.benchmark(threads=4)
    def bench_x(state):
        for _ in state:
            pass

    assert mew.run(min_time="1x", reporter=Capture()) == 1
    assert warmed == [True]


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_threaded_benchmark_runs_without_deadlock():
    """Real threaded run on a free-threaded build. Regression guard for the
    stop-the-world attach deadlock: if it returns, the warmup did its job (a
    failure here manifests as a hang / CI timeout)."""

    @mew.benchmark(threads=4, iterations=100)
    def bench_x(state):
        for _ in state:
            pass
        state.set_counter("nthreads", state.threads)

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert len(cap.runs) == 1
    assert cap.runs[0]["threads"] == 4


def test_run_multiple_reporters_fan_out():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    a = Capture()
    b = Capture()
    mew.run(min_time="1x", reporter=[a, b])
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
    mew.run(reporter=cap)
    assert cap.runs[0]["iterations"] == 42


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
    mew.run(min_time="1x", reporter=cap, filter="bench_a")
    names = [r["name"] for r in cap.runs]
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
    mew.run(reporter=cap, filter=".*")
    assert cap.runs[0]["iterations"] == 1
    assert cap.runs[1]["iterations"] == 1
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
    mew.run(reporter=cap, filter=".*")
    unpaused, paused = cap.runs
    paused_time = paused["real_accumulated_time"]
    unpaused_time = unpaused["real_accumulated_time"]
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
    mew.run(reporter=cap, filter=".*")
    assert cap.runs[0]["iterations"] == 1


def test_run_with_no_entries_returns_zero():
    cap = Capture()
    assert mew.run(min_time="1x", reporter=cap) == 0
    assert cap.runs == []


def test_state_batches_drives_body_in_multiples_of_n():
    body_calls: list[int] = []

    @mew.benchmark(iterations=10)
    def bench_batched(state):
        for n in state.batches(4):
            for _ in range(n):
                body_calls.append(1)

    cap = Capture()
    mew.run(reporter=cap, filter=".*")
    # 3 batches × 4 = 12 body calls; GB reports the actual count, not the cap.
    assert cap.runs[0]["iterations"] == 12
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
    mew.run(reporter=cap, filter=".*")
    assert seen == [ValueError]


def test_state_range_out_of_bounds_raises():
    # GB's own guard is an assert (compiled out in Release); the binding must
    # raise instead of reading past the range vector.
    seen: list[type] = []

    @mew.benchmark(iterations=1)
    def bench_norange(state):
        try:
            state.range(0)  # not parametrized: no range arguments
        except IndexError as e:
            seen.append(type(e))
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert seen == [IndexError]


def test_keyboard_interrupt_stops_run_and_propagates():
    """KeyboardInterrupt/SystemExit in a body must abort the run, not become a
    skipped row while the remaining benchmarks execute."""
    bodies: list[str] = []

    @mew.benchmark(iterations=1)
    def bench_a_interrupts(state):
        bodies.append("a")
        for _ in state:
            pass
        raise KeyboardInterrupt

    @mew.benchmark(iterations=1)
    def bench_b_never_runs(state):
        bodies.append("b")
        for _ in state:
            pass

    cap = Capture()
    with pytest.raises(KeyboardInterrupt):
        mew.run(min_time="1x", reporter=cap)
    assert bodies == ["a"]

    # The interrupt is consumed: a follow-up run starts clean and completes.
    @mew.benchmark(iterations=1)
    def bench_c(state):
        for _ in state:
            pass

    cap2 = Capture()
    entries = [e for e in mew._registry.REGISTRY.all() if "bench_c" in e.name]
    assert mew.run(entries, min_time="1x", reporter=cap2) == 1
    assert [r["skipped"] for r in cap2.runs] == [False]


def test_benchmark_body_stderr_is_visible(capfd: pytest.CaptureFixture[str]):
    # fd 2 must stay live during the run: only GB's system-info probes are
    # silenced, not user output from benchmark bodies.
    @mew.benchmark(iterations=1)
    def bench_noisy(state):
        for _ in state:
            pass
        print("body stderr marker", file=sys.stderr, flush=True)

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert "body stderr marker" in capfd.readouterr().err
