"""End-to-end: register via Python API, run via the C++ runner, capture results."""

from __future__ import annotations

import gc
import json
import platform
import sys
import time
import weakref

import pytest
from _helpers import Capture

import mew


def test_native_registry_keeps_the_callable_alive_until_cleared():
    """A native registration owns its callable, even without a Python-side registry entry."""
    from mew import _core

    calls = []

    def bench(state):
        calls.append("ran")
        for _ in state:
            pass

    ref = weakref.ref(bench)
    _core.clear_registered_benchmarks()
    try:
        _core.register_benchmark("owned_callable", bench).iterations(1).repetitions(1)
        del bench
        gc.collect()
        assert ref() is not None
        _core.run_benchmarks(["mew"], Capture())
        assert calls == ["ran"]
    finally:
        _core.clear_registered_benchmarks()
    gc.collect()
    assert ref() is None


def test_run_reports_rows_and_the_session_context():
    """A run reports one row per benchmark and a context with mew's session and Python details."""

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    assert mew.run(min_time="3x", reporter=cap) == 1
    (row,) = cap.runs
    assert row["iterations"] == 3
    assert cap.finalized
    # The context is what mew assembled; Google Benchmark's own adds nothing.
    assert cap.context is not None
    assert set(cap.context) == {"session", "context"}
    # Rows from several interpreters or mew releases stay apart in one archive.
    assert cap.context["session"]["mew_version"] == mew.__version__
    assert cap.context["context"]["python"] == {
        "version": platform.python_version(),
        "implementation": sys.implementation.name,
        "free_threaded_build": mew.machine._is_free_threaded_build(),
        "gil_enabled": mew.machine._gil_enabled(),
    }


# The validators are shared with the decorators, whose tests cover their cases;
# one case per option shows that run() calls them.
@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"repetitions": 0}, ValueError, "repetitions must be >= 1"),
        ({"memory_iterations": 0}, ValueError, "memory_iterations must be >= 1"),
        ({"min_time": "forever"}, ValueError, "min_time must be positive"),
        ({"min_warmup_time": -1}, ValueError, "min_warmup_time must be a finite"),
        ({"session_tag": 1}, TypeError, "session_tag must be a string"),
        ({"session_tag": ""}, ValueError, "session_tag must not be empty"),
        # `mew compare path@latest` selects the newest session.
        ({"session_tag": "latest"}, ValueError, "session_tag 'latest' is reserved"),
    ],
)
def test_run_rejects_invalid_options(kwargs, error, message):
    """`mew.run` rejects invalid option types and values with a message naming the option."""
    with pytest.raises(error, match=message):
        mew.run(**kwargs)


@pytest.mark.parametrize(
    ("value", "flag"),
    [
        (0.5, "0.5s"),
        ("0.5", "0.5s"),
        ("0.5s", "0.5s"),
        # Units Google Benchmark doesn't take.
        ("500ms", "0.5s"),
        ("1m", "60.0s"),
        (" 7x ", "7x"),
    ],
)
def test_parse_min_time_renders_google_benchmark_syntax(value, flag):
    """`parse_min_time` turns seconds, unit strings and `Nx` into Google Benchmark's syntax."""
    from mew._options import parse_min_time

    assert parse_min_time(value) == flag


def test_a_benchmarks_own_warmup_needs_no_min_time_of_its_own():
    """A benchmark's `min_warmup_time` takes effect without a `min_time` on the benchmark."""

    @mew.benchmark(min_warmup_time=0.2)
    def bench_warm(state):
        for _ in state:
            pass

    start = time.perf_counter()
    mew.run(min_time="0.01s", reporter=Capture())
    assert time.perf_counter() - start >= 0.2


def test_gb_flags_do_not_leak_across_runs():
    """`repetitions` from one run does not carry over to the next run in the same process."""

    @mew.benchmark
    def bench_leak(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", repetitions=2, reporter=cap)
    assert len(cap.runs) == 2

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)  # no repetitions requested
    assert len(cap.runs) == 1


def test_set_counter_values_reach_rows_with_their_format():
    """`state.set_counter` values reach rows normalized by their flags, with `one_k` as format."""

    @mew.benchmark(iterations=4)
    def bench_counter(state):
        for _ in state:
            pass
        state.set_counter("bytes", 1024, one_k=mew.CounterOneK.kIs1024)
        state.set_counter("total", 3, flags=mew.CounterFlags.kIsIterationInvariant)
        state.set_counter("mean", 8, flags=mew.CounterFlags.kAvgIterations)

    cap = Capture()
    mew.run(reporter=cap)
    # Values are normalized by their flags (x4, /4); one_k only affects display.
    assert cap.runs[0]["counters"] == [
        {"name": "bytes", "value": 1024, "format": {"one_k": 1024}},
        {"name": "mean", "value": 2},
        {"name": "total", "value": 12},
    ]


def test_run_registers_only_selected_cases_of_a_family():
    """A -k-narrowed family runs only the selected cases, with their kwargs bound."""
    seen_n = []

    @mew.parametrize([{"n": 1}, {"n": 10}, {"n": 100}], ids=["small", "mid", "big"])
    def bench_fam(state, n):
        seen_n.append(n)
        for _ in state:
            pass

    narrowed = mew.REGISTRY.filter(r"small|big")
    cap = Capture()
    mew.run(narrowed, min_time="1x", reporter=cap)

    family = narrowed[0].name
    assert [r["name"] for r in cap.runs] == [f"{family}[small]", f"{family}[big]"]
    assert seen_n == [1, 100]


@pytest.fixture
def threaded_bench_on_gil_build(monkeypatch):
    """A GIL build (simulated) with one threaded benchmark registered."""
    monkeypatch.setattr("mew._options._gil_enabled", lambda: True)

    # Any count above 1 makes the benchmark threaded.
    @mew.benchmark(threads=[1, 4], iterations=1)
    def bench_threaded(state):
        for _ in state:
            pass


@pytest.mark.usefixtures("threaded_bench_on_gil_build")
def test_threaded_benchmark_skipped_on_gil_build():
    """Threaded mode would deadlock on GB's start barrier under the GIL, so mew
    warns and drops the benchmark, without a row."""
    cap = Capture()
    with pytest.warns(RuntimeWarning, match="skipping 1 threaded benchmark"):
        assert mew.run(min_time="1x", reporter=cap) == 0
    assert cap.runs == []


@pytest.mark.usefixtures("threaded_bench_on_gil_build")
def test_threaded_benchmark_strict_raises_on_gil_build():
    """`strict=True` errors so CI cannot mask a misconfiguration with a skip."""
    with pytest.raises(RuntimeError, match="free-threaded interpreter"):
        mew.run(min_time="1x", reporter=Capture(), strict=True)


@pytest.mark.usefixtures("threaded_bench_on_gil_build")
def test_mixed_suite_skips_threaded_runs_rest_on_gil_build():
    """Only threaded benchmarks are dropped; the rest run."""

    @mew.benchmark(iterations=1)
    def bench_plain(state):
        for _ in state:
            pass

    cap = Capture()
    with pytest.warns(RuntimeWarning, match="threaded"):
        assert mew.run(min_time="1x", reporter=cap) == 1
    assert [r["name"].rsplit(".", 1)[-1] for r in cap.runs] == ["bench_plain"]


@pytest.mark.skipif(
    mew.machine._gil_enabled(),
    reason="threaded mode requires a free-threaded interpreter",
)
def test_thread_counts_run_each_requested_count(monkeypatch):
    """A real threaded run on a free-threaded build; a failure here is a hang."""
    from mew import _core

    # mew warms up free threading before GB starts its workers. Skipping that
    # doesn't always deadlock, so the run alone can't show it happened.
    warmups: list[None] = []
    warmup = _core.warmup_free_threading

    def spy() -> None:
        warmups.append(None)
        warmup()

    monkeypatch.setattr(_core, "warmup_free_threading", spy)

    @mew.benchmark(threads=[1, 3, 5], iterations=10)
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert [row["threads"] for row in cap.runs] == [1, 3, 5]
    assert all(row["skip"] is None for row in cap.runs)
    assert len(warmups) == 1


def test_run_multiple_reporters_fan_out():
    """Each reporter in a `reporter` list receives every row and is finalized."""

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


def test_manual_time_replaces_the_measured_real_time():
    """With `use_manual_time`, times from `state.set_iteration_time` replace the real time."""

    @mew.benchmark(iterations=4, use_manual_time=True)
    def bench_manual(state):
        for _ in state:
            state.set_iteration_time(0.5)

    cap = Capture()
    mew.run(reporter=cap)
    (row,) = cap.runs
    assert row["iterations"] == 4
    # 4 x 0.5 s reported over 4 iterations; the empty body's wall time plays no part.
    assert row["real_time"] == 0.5e9


def test_pause_scope_is_only_created_by_state_pause():
    """`PauseScope` cannot be constructed directly from Python."""
    from mew._core import PauseScope

    # A scope without a State would dereference null on entry.
    with pytest.raises(TypeError, match="cannot create"):
        PauseScope()


def _burn(seconds: float) -> None:
    """Spend ``seconds`` of this thread's CPU time. The tests bound the CPU time a
    timer counted, which, unlike real time, can't grow while the OS stalls the
    thread, so only the timer's state decides the result."""
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def test_state_pause_resumes_on_exception():
    """An exception that leaves a `state.pause()` scope resumes the timer."""

    @mew.benchmark(iterations=1)
    def bench_raises(state):
        for _ in state:
            try:
                with state.pause():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            _burn(0.05)  # timed only if __exit__ resumed the timer

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert row["iterations"] == 1
    assert row["cpu_time"] >= 0.04e9


def test_an_exception_skips_with_its_type_and_message(capfd):
    """A raising benchmark records `Type: message`, which has no paths to leak from a shared
    result file; the traceback goes to stderr."""

    @mew.benchmark(iterations=1)
    def bench_raises(state):
        for _ in state:
            json.loads("{")

    @mew.benchmark(iterations=1)
    def bench_asserts(state):
        for _ in state:
            raise AssertionError

    cap = Capture()
    mew.run(reporter=cap)
    messages = {r["name"].rsplit(".", 1)[-1]: r["skip"]["message"] for r in cap.runs}
    assert messages == {
        "bench_asserts": "AssertionError",
        "bench_raises": "json.decoder.JSONDecodeError: Expecting property name enclosed in "
        "double quotes: line 1 column 2 (char 1)",
    }
    err = capfd.readouterr().err
    assert "Traceback (most recent call last):" in err
    assert 'json.loads("{")' in err


@pytest.mark.parametrize("reuse_scope", [False, True])
def test_nested_state_pause_keeps_the_outer_scope_paused(reuse_scope):
    """A nested pause keeps timing stopped until the outer scope exits; re-entering one raises."""
    errors: list[str] = []

    @mew.benchmark(iterations=1)
    def bench_nested(state):
        for _ in state:
            outer = state.pause()
            with outer:
                try:
                    with outer if reuse_scope else state.pause():
                        pass
                except RuntimeError as exc:
                    errors.append(str(exc))
                # Exiting the inner scope must not resume timing yet.
                _burn(0.2)

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    assert row["skip"] is None
    # A fresh inner scope does nothing; re-entering the outer one raises.
    reentry = "this pause scope is already active and cannot be re-entered"
    assert errors == ([reentry] if reuse_scope else [])
    # A resumed timer counts the whole burn, >= 0.2 s.
    assert row["cpu_time"] < 0.1e9


@pytest.mark.skipif(mew.machine._gil_enabled(), reason="requires free-threaded Python")
def test_nested_state_pauses_are_independent_between_workers():
    """Threaded workers each hold their own nested pauses, and no worker's burn is timed."""
    from threading import Barrier

    barrier = Barrier(2)

    @mew.benchmark(iterations=1, threads=2)
    def bench_nested(state):
        for _ in state:
            with state.pause(), state.pause():
                # Both workers hold their pause at once; a pause shared between
                # them would leave one worker's timer running through the burn.
                barrier.wait(timeout=5)
                _burn(0.2)

    cap = Capture()
    mew.run(reporter=cap)
    (row,) = cap.runs
    assert row["skip"] is None
    assert row["iterations"] == 2
    # CPU time sums both workers' timers: one timed burn is >= 0.2 s over 2 iterations.
    assert row["cpu_time"] < 0.05e9


def test_leaving_the_loop_early_marks_the_run_as_incomplete():
    """A `break` or `return` out of the benchmark loop skips the row as an incomplete loop."""

    # One iteration makes the first the final one, whose iteration count is
    # already complete: only the running timer shows the loop was left.
    @mew.benchmark(iterations=1)
    def bench_breaks(state):
        for _ in state:
            break

    @mew.benchmark(iterations=1)
    def bench_returns(state):
        for _ in state.batches(7):
            return

    cap = Capture()
    mew.run(reporter=cap)
    messages = [r["skip"]["message"] for r in cap.runs]
    assert messages == ["The benchmark did not complete its loop."] * 2


@pytest.mark.parametrize("where", ["before", "after"])
def test_state_pause_outside_the_loop_skips_with_error(where):
    """`state.pause()` before or after the benchmark loop skips the row with an error."""

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
    assert cap.runs[0]["skip"] is not None
    assert "only valid inside the benchmark loop" in cap.runs[0]["skip"]["message"]


def test_state_skip_inside_pause_does_not_resume_timing():
    """A skip inside a `state.pause()` scope skips the row with its message. The bug it guards,
    timing resumed after the skip, only fails Debug builds (the ASAN job), see below."""

    # A resume after the skip trips the assert in Google Benchmark's
    # ResumeTiming(), which only Debug builds (the ASAN job) compile in; a
    # release build reads no timer for a skipped row, so this passes either way.
    @mew.benchmark(iterations=1)
    def bench_x(state):
        for _ in state:
            with state.pause():
                state.skip_with_message("unmet precondition")

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skip"] is not None
    assert cap.runs[0]["skip"]["message"] == "unmet precondition"


@pytest.mark.parametrize("loop", ["iter", "batches"])
def test_pause_scope_held_open_across_the_loop_end_stays_untimed(loop):
    """An ExitStack around the loop keeps its pause scope open over the final
    loop step: GB must not stop the stopped timer again, and the paused time
    must stay out of the result."""
    from contextlib import ExitStack

    @mew.benchmark(iterations=1)
    def bench_held(state):
        with ExitStack() as stack:
            for _ in state if loop == "iter" else state.batches(1):
                stack.enter_context(state.pause())
                _burn(0.2)

    cap = Capture()
    mew.run(reporter=cap)
    (row,) = cap.runs
    assert row["skip"] is None
    # Stopping the timer again adds the time since the loop started, >= 0.2 s.
    assert row["cpu_time"] < 0.1e9


def test_pause_scope_left_open_after_a_break_does_not_leak():
    """A pause scope left open at a `break` does not change pause timing in the next benchmark."""

    @mew.benchmark(iterations=2)
    def bench_breaks(state):
        for _ in state:
            state.pause().__enter__()
            break

    @mew.benchmark(iterations=1)
    def bench_next(state):
        for _ in state:
            with state.pause():
                _burn(0.2)

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skip"]["message"] == "The benchmark did not complete its loop."
    # A leaked pause state would time the 0.2 s burn.
    assert cap.runs[1]["cpu_time"] < 0.1e9


def test_second_loop_over_a_finished_state_adds_no_time():
    """A second loop over a finished `state` does not add the time between the loops."""

    @mew.benchmark(iterations=5)
    def bench_x(state):
        for _ in state:
            pass
        _burn(0.2)  # untimed, unless the second loop stops the timer again
        for _ in state:
            pass

    cap = Capture()
    mew.run(reporter=cap)
    row = cap.runs[0]
    # Stopping the timer again adds the burn, >= 0.2 s in total.
    assert row["cpu_time"] * row["iterations"] < 0.1e9


def test_run_with_no_entries_returns_zero():
    """`mew.run` with no registered benchmarks returns 0 and reports no rows."""
    cap = Capture()
    assert mew.run(min_time="1x", reporter=cap) == 0
    assert cap.runs == []


def test_state_batches_yields_n_and_counts_the_overshoot():
    """`state.batches(n)` yields `n` per batch, and the row counts the last batch's overshoot."""
    yielded: list[int] = []

    @mew.benchmark(iterations=10)
    def bench_batched(state):
        yielded.extend(state.batches(4))

    cap = Capture()
    mew.run(reporter=cap)
    # Three batches of 4 cover the 10 iterations; GB reports all 12 that ran.
    assert yielded == [4, 4, 4]
    assert cap.runs[0]["iterations"] == 12


def test_state_rejects_a_batch_size_below_one():
    """`state.batches(0)` raises and skips the row."""

    # GB's own guard is an assert, compiled out in Release: a batch of 0 never ends the loop.
    @mew.benchmark(iterations=1)
    def bench_batches(state):
        state.batches(0)

    cap = Capture()
    mew.run(reporter=cap)
    assert cap.runs[0]["skip"]["message"] == "ValueError: batch size must be positive"


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
    def bench_b(state):
        bodies.append("b")
        for _ in state:
            pass

    cap = Capture()
    with pytest.raises(KeyboardInterrupt):
        mew.run(reporter=cap)
    assert bodies == ["a"]
    # Benchmarks skipped by the abort must not reach result files as rows.
    assert all(r["skip"] is None or r["skip"]["message"] != "aborted" for r in cap.runs)

    # The interrupt is consumed: a follow-up run starts clean and completes.
    cap = Capture()
    assert mew.run(mew.REGISTRY.filter("bench_b"), reporter=cap) == 1
    assert [r["skip"] for r in cap.runs] == [None]


def test_reporter_failure_aborts_the_run():
    """A broken sink aborts the suite rather than measuring results that get discarded."""
    bodies: list[int] = []

    @mew.parametrize([{"n": 1}, {"n": 2}, {"n": 3}], iterations=1)
    def bench_x(state, n):
        bodies.append(n)
        for _ in state:
            pass

    class Exploding(Capture):
        def report_runs(self, runs):
            raise RuntimeError("sink is broken")

    with pytest.raises(RuntimeError, match="sink is broken"):
        mew.run(reporter=Exploding())
    assert bodies == [1], "later benchmarks must not run once the sink failed"

    # The abort slot is cleared by the rethrow, so a follow-up run starts clean.
    cap = Capture()
    assert mew.run(reporter=cap) == 3
    assert [r["skip"] for r in cap.runs] == [None] * 3


def test_report_context_return_value_is_ignored():
    """A falsy return must not veto the run; GB would report success with no rows."""

    class ReturnsFalse(Capture):
        def report_context(self, context):
            super().report_context(context)
            return False

    @mew.benchmark(iterations=1)
    def bench_v(state):
        for _ in state:
            pass

    rep = ReturnsFalse()
    assert mew.run(reporter=rep) == 1
    assert rep.runs, "rows must flow: the return value carries no meaning"


# --- output capture -----------------------------------------------------------
# Writes go through os.write: under pytest, print() targets pytest's own
# sys.stdout replacement, while native libraries write to the descriptors.


def test_benchmark_output_is_printed_to_stderr_after_the_run(capfd):
    """Output from benchmark bodies goes to stderr after the run, not into rows; reporter writes
    reach stdout directly."""
    import os

    class Recording(Capture):
        def report_runs(self, runs):
            # Reporters write to the real streams while the capture is suspended.
            os.write(1, b"reporter row\n")
            super().report_runs(runs)

    @mew.benchmark(iterations=1)
    def bench_noisy(state):
        os.write(1, b"to stdout\n")
        os.write(2, b"to stderr\n")
        for _ in state:
            pass

    rep = Recording()
    mew.run(reporter=rep)
    out, err = capfd.readouterr()
    assert out == "reporter row\n"
    assert err == "\noutput from benchmarks:\nto stdout\nto stderr\n"


def test_longest_row_name_covers_cases_and_threads():
    """`longest_row_name` counts case labels, only selected cases, and the `/threads:N` suffix."""
    from mew._registry import Entry, longest_row_name

    def entry(name, **kwargs):
        return Entry(name=name, fn=lambda s: None, **kwargs)

    plain = entry("f.py::bench_a")
    family = entry("f.py::bench_b", case_labels=["n=1", "n=1000"])
    assert longest_row_name([plain]) == len("f.py::bench_a")
    assert longest_row_name([family]) == len("f.py::bench_b[n=1000]")
    # Only the selected cases, and `/threads:N` for the largest count.
    narrowed = entry("f.py::bench_b", case_labels=["n=1", "n=1000"], cases=[0])
    assert longest_row_name([narrowed]) == len("f.py::bench_b[n=1]")
    threaded = entry("f.py::bench_c", options={"threads": (1, 16)})
    assert longest_row_name([threaded]) == len("f.py::bench_c/threads:16")


def test_run_sizes_the_console_name_column_to_the_longest_row(capsys, monkeypatch):
    """The console table's name column is as wide as the longest row name."""
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")

    @mew.parametrize([{"n": 1}, {"n": 1000}], iterations=1)
    def bench_family(state, n):
        for _ in state:
            pass

    mew.run()
    lines = capsys.readouterr().out.splitlines()
    names = [line.split(" │ ")[0] for line in lines[1:] if " │ " in line]
    # The column is just wide enough for the longest name.
    assert len(names[0]) == max(len(name.rstrip()) for name in names[1:])
    assert any(name.rstrip().endswith("[n=1000]") for name in names[1:])


@pytest.mark.parametrize("reporter", [None, []])
def test_run_without_reporter_prints_mews_console_table(capsys, monkeypatch, reporter):
    """With no reporters, `mew.run` prints mew's console table, not Google Benchmark's."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    @mew.benchmark(iterations=1)
    def bench_plain(state):
        for _ in state:
            pass

    assert mew.run(reporter=reporter) == 1
    out = capsys.readouterr().out
    assert out.startswith("mew · cpu=")
    # The header reads the machine context mew.run() assembled.
    assert "cpus=?" not in out
    assert "bench_plain" in out
    # Google Benchmark's own console reporter prints a "Run on (...)" header.
    assert "Run on (" not in out
