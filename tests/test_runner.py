"""End-to-end: register via Python API, run via the C++ runner, capture results."""

from __future__ import annotations

import sys

import pytest
from _helpers import Capture

import mew


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
    assert cap.context["context"]["num_cpus"] >= 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repetitions": 0}, "repetitions"),
        ({"repetitions": True}, "repetitions"),
        ({"min_warmup_time": -1}, "min_warmup_time"),
        ({"min_warmup_time": float("nan")}, "min_warmup_time"),
        ({"min_time": 0}, "min_time"),
        ({"min_time": "0x"}, "min_time"),
        ({"min_time": "1.5x"}, "min_time"),
        ({"min_time": "forever"}, "min_time"),
        ({"memory_iterations": 0}, "memory_iterations"),
        ({"memory_iterations": True}, "memory_iterations"),
    ],
)
def test_run_rejects_invalid_global_options(kwargs, message):
    with pytest.raises(ValueError, match=message):
        mew.run(**kwargs)


def test_gb_argv_always_pins_the_memory_pass_cap():
    from mew.runner import _gb_argv

    # Every flag is emitted so a previous run's value cannot leak into this one.
    assert "--benchmark_memory_iterations=16" in _gb_argv(None, None, None, False)
    assert "--benchmark_memory_iterations=4" in _gb_argv(None, None, None, False, 4)


def test_set_counter_values_reach_rows_normalized_by_flags():
    @mew.benchmark(iterations=4)
    def bench_counter(state):
        for _ in state:
            pass
        # one_k only affects GB's console formatting; rows carry the raw value.
        state.set_counter("bytes", 1024, one_k=mew.CounterOneK.kIs1024)
        state.set_counter("total", 3, flags=mew.CounterFlags.kIsIterationInvariant)
        state.set_counter("mean", 8, flags=mew.CounterFlags.kAvgIterations)

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["iterations"] == 4
    assert cap.runs[0]["counters"] == {"bytes": 1024, "total": 12, "mean": 2}


def test_run_benchmarks_passes_extra_context_through():
    """`extra_context` *is* the context block: Google Benchmark's own carries
    nothing mew keeps, so the binding forwards what the caller assembled."""
    from mew import _core

    def bench(state):
        for _ in state:
            pass

    _core.clear_registered_benchmarks()
    _core.register_benchmark("bench_overlay", bench)
    try:
        cap = Capture()
        extra = {"session": {"id": "sid-123", "host": "h"}, "context": {"k": "v"}}
        _core.run_benchmarks(["mew", "--benchmark_min_time=1x"], cap, extra)
    finally:
        _core.clear_registered_benchmarks()

    assert cap.context is not None
    assert cap.context == {"session": {"id": "sid-123", "host": "h"}, "context": {"k": "v"}}


def test_run_registers_only_selected_cases_of_a_family():
    """A -k-narrowed family runs only the selected cases, with their kwargs bound."""
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
    assert sorted(n.split("/case:")[1] for n in case_names) == ["0", "2"]
    assert sorted(seen_n) == [1, 100]


def test_is_threaded_helper():
    from mew.runner import _is_threaded

    assert not _is_threaded({})
    assert not _is_threaded({"threads": 1})
    assert _is_threaded({"threads": 2})
    assert not _is_threaded({"threads": (1,)})
    assert _is_threaded({"threads": (1, 8)})


def test_thread_counts_are_each_applied_to_native_handle():
    from mew.runner import _apply_options

    calls = []

    class Handle:
        def threads(self, n):
            calls.append(n)

    _apply_options(Handle(), {"threads": (2, 4, 8)})  # ty: ignore[invalid-argument-type]
    assert calls == [2, 4, 8]


def test_threaded_benchmark_skipped_on_gil_build(monkeypatch):
    """Threaded mode would deadlock on GB's start barrier under the GIL, so mew
    warns and emits a skipped row instead."""
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
    """`strict=True` errors so CI cannot mask a misconfiguration with a skip."""
    from mew import runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: True)

    @mew.benchmark(threads=4)
    def bench_x(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="free-threaded interpreter"):
        mew.run(min_time="1x", reporter=Capture(), strict=True)


def test_all_skipped_finalizes_reporter_when_report_runs_raises(monkeypatch):
    """The Python-driven all-skipped lifecycle must close reporters on errors,
    just like Google Benchmark's normal reporter lifecycle does."""
    from mew import runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: True)

    @mew.benchmark(threads=2)
    def bench_x(state):
        for _ in state:
            pass

    class RaisingReporter(Capture):
        def report_runs(self, runs):
            raise RuntimeError("report failed")

    rep = RaisingReporter()
    with pytest.warns(RuntimeWarning), pytest.raises(RuntimeError, match="report failed"):
        mew.run(min_time="1x", reporter=rep)

    assert rep.finalized


def test_mixed_suite_skips_threaded_runs_rest_on_gil_build(monkeypatch):
    """Only threaded benchmarks are skipped. Skipped rows flush from `report_context`,
    the only callback guaranteed to fire after a sink opens and before it writes."""
    monkeypatch.setattr("mew.runner._gil_enabled", lambda: True)
    order: list[str] = []

    class Recording(Capture):
        def report_context(self, context):
            super().report_context(context)
            order.append("context")

        def report_runs(self, runs):
            super().report_runs(runs)
            order.extend("row:" + r["name"].rsplit(".", 1)[-1] for r in runs)

        def finalize(self) -> None:
            super().finalize()
            order.append("finalize")

    @mew.benchmark(threads=4, iterations=1)
    def bench_threaded(state):
        for _ in state:
            pass

    @mew.benchmark(iterations=1)
    def bench_plain(state):
        for _ in state:
            pass

    cap = Recording()
    with pytest.warns(RuntimeWarning, match="threaded"):
        n = mew.run(min_time="1x", reporter=cap)

    assert n == 1  # bench_plain ran
    threaded_rows = [r for r in cap.runs if "bench_threaded" in r["name"]]
    plain_rows = [r for r in cap.runs if "bench_plain" in r["name"]]
    assert threaded_rows and all(r["skipped"] for r in threaded_rows)
    assert plain_rows and not any(r["skipped"] for r in plain_rows)
    assert order[0] == "context"
    assert order[-1] == "finalize"
    # The skipped row lands first, ahead of anything Google Benchmark reports.
    assert order[1] == "row:bench_threaded"
    assert any(o.startswith("row:bench_plain") for o in order)


def test_threaded_benchmark_warms_up_on_free_threaded(monkeypatch):
    """On a free-threaded build the guard passes and mew warms the threading
    state before invoking the C++ runner (which we stub to avoid real threads)."""
    from mew import _core, runner

    monkeypatch.setattr(runner, "_gil_enabled", lambda: False)
    warmed = []
    monkeypatch.setattr(_core, "warmup_free_threading", lambda: warmed.append(True))
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
    """A real threaded run on a free-threaded build; a failure here is a hang."""

    @mew.benchmark(threads=4, iterations=100)
    def bench_x(state):
        for _ in state:
            pass
        state.set_counter("nthreads", state.threads)

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert len(cap.runs) == 1
    assert cap.runs[0]["skipped"] is False
    assert cap.runs[0]["threads"] == 4


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_thread_counts_run_each_requested_count():
    @mew.benchmark(threads=[1, 3, 5], iterations=10)
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert [row["threads"] for row in cap.runs] == [1, 3, 5]


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


def test_state_pause_returns_a_context_manager_entering_as_itself():
    from mew._core import PauseScope

    seen: list[object] = []

    @mew.benchmark(iterations=1)
    def bench_x(state):
        for _ in state:
            cm = state.pause()
            assert isinstance(cm, PauseScope)
            with cm as entered:
                seen.append(entered is cm)

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skipped"] is False
    assert seen == [True]


def test_state_pause_resumes_on_exception():
    import time

    @mew.benchmark(iterations=1)
    def bench_raises(state):
        for _ in state:
            try:
                with state.pause():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            time.sleep(0.05)  # timed only if __exit__ resumed the timer

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert row["iterations"] == 1
    assert row["time_unit"] == "ns"
    assert row["real_time"] >= 0.04e9


@pytest.mark.parametrize("reuse_scope", [False, True])
def test_nested_state_pause_keeps_the_outer_scope_paused(reuse_scope):
    import time

    @mew.benchmark(iterations=1)
    def bench_nested(state):
        for _ in state:
            outer = state.pause()
            with outer:
                with outer if reuse_scope else state.pause():
                    pass
                # Exiting the inner scope must not resume timing yet.
                time.sleep(0.2)

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert not row["skipped"]
    assert row["real_time"] < 0.1e9


def test_nested_state_pause_unwinds_after_an_exception():
    import time

    @mew.benchmark(iterations=2)
    def bench_nested(state):
        for _ in state:
            with state.pause():
                try:
                    with state.pause():
                        raise ValueError("inner pause")
                except ValueError:
                    pass
                time.sleep(0.1)  # the outer scope must still be paused here
            time.sleep(0.01)  # timed: the outer scope must have resumed

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert not row["skipped"]
    assert row["iterations"] == 2
    assert row["time_unit"] == "ns"
    # Per iteration: ~0.01 s timed; ~0.11 s if the inner exit resumed early,
    # ~0 if the outer exit never resumed.
    assert 0.008e9 <= row["real_time"] < 0.05e9


@pytest.mark.skipif(
    getattr(sys, "_is_gil_enabled", lambda: True)(), reason="requires free-threaded Python"
)
def test_nested_state_pauses_are_independent_between_workers():
    from threading import Barrier

    barrier = Barrier(2)

    @mew.benchmark(iterations=1, threads=2)
    def bench_nested(state):
        for _ in state:
            with state.pause(), state.pause():
                barrier.wait(timeout=5)

    cap = Capture()
    mew.run(reporter=cap)
    assert not cap.runs[0]["skipped"]
    assert cap.runs[0]["iterations"] == 2


@pytest.mark.parametrize("batch", [1, 7])
@pytest.mark.parametrize("exit_kind", ["break", "return", "return_paused"])
def test_leaving_the_last_iteration_marks_the_run_as_incomplete(batch, exit_kind):
    @mew.benchmark(iterations=1)
    def bench_incomplete(state):
        for _ in state.batches(batch):
            if exit_kind == "return_paused":
                state.pause().__enter__()
                return
            if exit_kind == "return":
                return
            break

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skipped"]
    assert cap.runs[0]["skip_message"] == "The benchmark did not complete its loop."


# mew reports the body's exception as unraisable after skipping the run.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("where", ["before", "after"])
def test_state_pause_outside_the_loop_skips_with_error(where):
    # Unchecked, this stops a timer that never ran and reports the raw clock.
    @mew.benchmark(iterations=1)
    def bench_x(state):
        if where == "before":
            with state.pause():
                pass
        for _ in state:
            pass
        if where == "after":
            with state.pause():
                pass

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skipped"] is True
    assert "only valid inside the benchmark loop" in cap.runs[0]["skip_message"]


def test_state_skip_inside_pause_does_not_resume_timing():
    @mew.benchmark(iterations=1)
    def bench_x(state):
        for _ in state:
            with state.pause():
                state.skip_with_message("unmet precondition")

    @mew.benchmark(iterations=1)
    def bench_y(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skipped"] is True
    assert cap.runs[0]["skip_message"] == "unmet precondition"
    assert cap.runs[1]["skipped"] is False


@pytest.mark.parametrize("loop", ["iter", "batches", "keep_running_batch"])
def test_pause_scope_held_open_across_the_loop_end_stays_untimed(loop):
    """An ExitStack around the loop keeps its pause scopes open over the final
    loop step: GB must not stop the stopped timer again, and the paused time
    must stay out of the result."""
    import time
    from contextlib import ExitStack

    @mew.benchmark(iterations=2)
    def bench_held(state):
        with ExitStack() as stack:

            def body():
                stack.enter_context(state.pause())
                time.sleep(0.02)

            if loop == "iter":
                for _ in state:
                    body()
            elif loop == "batches":
                for _ in state.batches(1):
                    body()
            else:
                while state.keep_running_batch(1):
                    body()

    @mew.benchmark(iterations=2)
    def bench_next(state):
        for _ in state:
            with state.pause():
                time.sleep(0.05)

    cap = Capture()
    mew.run(reporter=cap)
    for row in cap.runs:
        assert row["skipped"] is False
        assert row["real_time"] < 0.01e9  # ns; the sleeps were paused


def test_pause_scope_left_open_after_a_break_does_not_leak():
    import time

    @mew.benchmark(iterations=2)
    def bench_breaks(state):
        for _ in state:
            state.pause().__enter__()
            break

    @mew.benchmark(iterations=2)
    def bench_next(state):
        for _ in state:
            with state.pause():
                time.sleep(0.05)

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skip_message"] == "The benchmark did not complete its loop."
    assert cap.runs[1]["real_time"] < 0.01e9


def test_second_loop_over_a_finished_state_adds_no_time():
    import time

    @mew.benchmark(iterations=5)
    def bench_x(state):
        for _ in state:
            pass
        time.sleep(0.2)  # untimed, unless the second loop stops the timer again
        for _ in state:
            pass

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert row["time_unit"] == "ns"
    assert row["real_time"] * row["iterations"] < 0.1e9


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
    mew.run(reporter=cap)
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
    mew.run(reporter=cap)
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
    # Benchmarks skipped by the abort must not reach result files as rows.
    assert all(r["skip_message"] != "aborted" for r in cap.runs)

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


def test_gb_flags_do_not_leak_across_runs():
    """GB flags are process-global: a knob set for one run() must not apply to
    the next run() in the same process that didn't ask for it."""

    @mew.benchmark
    def bench_leak(state):
        for _ in state:
            pass

    entries = mew.REGISTRY.all()
    cap = Capture()
    mew.run(entries, min_time="1x", repetitions=2, reporter=cap)
    per_rep = [r for r in cap.runs if not r.get("aggregate_name")]
    assert len(per_rep) == 2

    cap = Capture()
    mew.run(entries, min_time="1x", reporter=cap)  # no repetitions requested
    per_rep = [r for r in cap.runs if not r.get("aggregate_name")]
    assert len(per_rep) == 1


def test_reporter_failure_aborts_the_run():
    """A broken sink aborts the suite rather than measuring results that get discarded."""
    bodies: list[str] = []

    class Exploding:
        def report_context(self, context, /):
            pass

        def report_runs(self, runs, /):
            raise RuntimeError("sink is broken")

    for name in ("a", "b", "c"):

        def body(state, _n=name):
            bodies.append(_n)
            for _ in state:
                pass

        body.__name__ = body.__qualname__ = f"bench_{name}"
        mew.benchmark(iterations=1, name=f"bench_{name}")(body)

    with pytest.raises(RuntimeError, match="sink is broken"):
        mew.run(min_time="1x", reporter=Exploding())
    assert bodies == ["a"], "later benchmarks must not run once the sink failed"


def test_abort_is_consumed_between_runs():
    """The abort slot is cleared by the rethrow, so the next run starts clean."""

    class Exploding:
        def report_context(self, context, /):
            pass

        def report_runs(self, runs, /):
            raise RuntimeError("sink is broken")

    @mew.benchmark(iterations=1)
    def bench_x(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="sink is broken"):
        mew.run(min_time="1x", reporter=Exploding())

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert cap.runs, "a follow-up run must not inherit the previous abort"


def test_report_context_return_value_is_ignored():
    """A falsy return must not veto the run; GB would report success with no rows."""

    class ReturnsFalse:
        def report_context(self, context, /):
            return False

        def report_runs(self, runs, /):
            seen.extend(runs)

    seen: list = []

    @mew.benchmark(iterations=1)
    def bench_v(state):
        for _ in state:
            pass

    assert mew.run(min_time="1x", reporter=ReturnsFalse()) == 1
    assert seen, "rows must flow: the return value carries no meaning"


@pytest.mark.parametrize("min_time", [0.00001, "0.00001", "0.00001s", " 7x "])
def test_run_accepts_seconds_and_fixed_iteration_syntax(min_time):
    @mew.benchmark
    def bench_time_option(state):
        for _ in state:
            pass

    cap = Capture()
    assert mew.run(min_time=min_time, reporter=cap) == 1
    assert len(cap.runs) == 1
    assert cap.runs[0]["iterations"] > 0
    if min_time == " 7x ":
        assert cap.runs[0]["iterations"] == 7
