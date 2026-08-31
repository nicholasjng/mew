"""In-process profiling: the Google Benchmark memory/profiler managers."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

import mew
from mew.reporter import JSONLReporter, JSONReporter


class FakeMemoryManager:
    """Memory manager returning fixed figures, so the stamp path is testable
    without memray installed."""

    def __init__(self, **figures: int) -> None:
        self._figures = figures or {
            "peak_bytes": 1024,
            "total_bytes": 2048,
            "total_allocations": 5,
        }
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> mew.MemoryMetrics:
        self.stops += 1
        return cast(mew.MemoryMetrics, self._figures.copy())


class FakeProfilerManager:
    """Profiler manager returning a fixed summary, standing in for pyinstrument."""

    _DEFAULT = {  # noqa: RUF012
        "profiler": "pyinstrument",
        "wall_time": 0.5,
        "sample_count": 500,
        "top_function": "foo (bar.py:1)",
        "top_function_total_self_time": 0.3,
    }

    def __init__(self, result: dict | None = _DEFAULT) -> None:
        # Default sentinel, not `None`: `result=None` means "report nothing".
        self._result = result
        self.starts = 0
        self.stops = 0
        self.pauses = 0

    def after_setup_start(self) -> None:
        self.starts += 1

    def before_teardown_stop(self) -> None:
        self.stops += 1

    def pause(self) -> None:
        self.pauses += 1

    def resume(self) -> None:
        pass

    def get_result(self) -> dict | None:
        return self._result


# --- manager registration and the Run stamp ----------------------------------


def test_memory_manager_is_driven_and_stamped_onto_rows(tmp_path):
    @mew.benchmark
    def bench_mem(state):
        for _ in state:
            pass

    mgr = FakeMemoryManager()
    out = tmp_path / "out.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out), memory_manager=mgr)

    # GB ran the extra memory pass and asked the manager for figures.
    assert mgr.starts >= 1
    assert mgr.stops == mgr.starts
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert bench["memory"]["peak_bytes"] == 1024
    assert bench["memory"]["total_bytes"] == 2048
    assert bench["memory"]["total_allocations"] == 5
    # memory_iterations is GB's own min(16, iters), and the per-iteration rate
    # is derived from it rather than supplied by the manager.
    assert bench["memory"]["iterations"] >= 1
    assert bench["memory"]["allocations_per_iteration"] == pytest.approx(
        5 / bench["memory"]["iterations"]
    )


def test_profiler_manager_is_driven_and_stamped_onto_rows(tmp_path):
    @mew.benchmark
    def bench_cpu(state):
        for _ in state:
            pass

    mgr = FakeProfilerManager()
    out = tmp_path / "out.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out), profiler_manager=mgr)

    assert mgr.starts >= 1
    assert mgr.stops == mgr.starts
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert bench["cpu_profile"] == {
        "profiler": "pyinstrument",
        "wall_time": 0.5,
        "sample_count": 500,
        "top_function": "foo (bar.py:1)",
        "top_function_total_self_time": 0.3,
    }


def test_profiler_manager_returning_none_leaves_row_unannotated(tmp_path):
    @mew.benchmark
    def bench_nosamples(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
        profiler_manager=FakeProfilerManager(result=None),
    )
    assert "cpu_profile" not in json.loads(out.read_text())["benchmarks"][0]


def test_profiler_manager_get_result_is_optional(tmp_path):
    class MinimalProfilerManager:
        def after_setup_start(self) -> None:
            pass

        def before_teardown_stop(self) -> None:
            pass

    @mew.benchmark
    def bench_minimal(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
        profiler_manager=MinimalProfilerManager(),
    )
    assert "cpu_profile" not in json.loads(out.read_text())["benchmarks"][0]


def test_profiler_manager_is_suspended_across_state_pause(tmp_path):
    @mew.benchmark
    def bench_paused(state):
        for _ in state:
            with state.pause():
                pass

    mgr = FakeProfilerManager()
    mew.run(min_time="10x", reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=mgr)
    # `state.pause()` must suspend the sampler, so setup inside the region is
    # excluded from the CPU profile as it is from the timing.
    assert mgr.pauses >= 1


def test_managers_do_not_leak_into_a_later_run(tmp_path):
    @mew.benchmark
    def bench_leak(state):
        for _ in state:
            pass

    mgr = FakeMemoryManager()
    mew.run(min_time="1x", reporter=JSONReporter(output=tmp_path / "a.json"), memory_manager=mgr)
    after_first = mgr.starts

    out = tmp_path / "b.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    # GB's manager registration is process-global; the scope must unregister it.
    assert mgr.starts == after_first
    assert "memory" not in json.loads(out.read_text())["benchmarks"][0]


def test_manager_exception_propagates_out_of_run(tmp_path):
    class Exploding(FakeMemoryManager):
        def stop(self):
            raise RuntimeError("capture unreadable")

    @mew.benchmark
    def bench_boom(state):
        for _ in state:
            pass

    with pytest.raises(RuntimeError, match="capture unreadable"):
        mew.run(
            min_time="1x",
            reporter=JSONReporter(output=tmp_path / "o.json"),
            memory_manager=Exploding(),
        )


@pytest.mark.parametrize("kind", ["memory", "profiler"])
def test_malformed_manager_result_propagates_out_of_run(tmp_path, kind):
    """Python result conversion belongs to the guarded native callback boundary."""

    @mew.benchmark
    def bench_bad_result(state):
        for _ in state:
            pass

    kwargs: dict[str, Any]
    if kind == "memory":

        class BadMemory(FakeMemoryManager):
            def stop(self):
                return {"peak_bytes": "not an integer"}

        kwargs = {"memory_manager": BadMemory()}
    else:

        class BadProfiler(FakeProfilerManager):
            def get_result(self):
                return {"sample_count": object()}

        kwargs = {"profiler_manager": BadProfiler()}

    with pytest.raises(TypeError):
        mew.run(
            min_time="1x",
            reporter=JSONReporter(output=tmp_path / "bad.json"),
            **kwargs,
        )


def test_reporters_omit_profile_blocks_without_managers(tmp_path):
    @mew.benchmark
    def bench_plain(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert "memory" not in bench
    assert "cpu_profile" not in bench


def test_jsonl_reporter_emits_profile_blocks(tmp_path):
    @mew.benchmark
    def bench_jl(state):
        for _ in state:
            pass

    out = tmp_path / "out.jsonl"
    mew.run(
        min_time="1x",
        reporter=JSONLReporter(output=out),
        memory_manager=FakeMemoryManager(),
        profiler_manager=FakeProfilerManager(),
    )
    row = json.loads(out.read_text().splitlines()[0])
    assert row["memory"]["peak_bytes"] == 1024
    assert row["cpu_profile"]["sample_count"] == 500


# --- the real backends -------------------------------------------------------


def test_memray_manager_reports_loop_allocations(tmp_path):
    """Scoped to the timing loop: a large fixture allocated before the loop must
    not appear in the tracked figures."""
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    setup_bytes = 50_000_000

    @mew.benchmark
    def bench_setup_heavy(state):
        fixture = bytearray(setup_bytes)  # setup: outside the capture window
        data = None
        for _ in state:
            data = bytearray(10_000)
        del fixture, data

    out = tmp_path / "out.json"
    with ExitStack() as stack:
        mew.run(
            min_time="20x",
            reporter=JSONReporter(output=out),
            memory_manager=_memory.manager(stack),
        )
    mem = json.loads(out.read_text())["benchmarks"][0]["memory"]
    assert mem["total_bytes"] < setup_bytes / 10
    assert mem["allocations_per_iteration"] == pytest.approx(
        mem["total_allocations"] / mem["iterations"]
    )


def test_pyinstrument_manager_summarizes_the_hot_frame(tmp_path):
    pytest.importorskip("pyinstrument")
    from mew import cpu as _cpu

    def spin() -> int:
        # Self time must land in `spin` itself, so loop in Python rather than
        # deferring to a C builtin like sum().
        total = 0
        for i in range(20_000):
            total += i
        return total

    @mew.benchmark
    def bench_hot(state):
        for _ in state:
            spin()

    out = tmp_path / "out.json"
    mgr = _cpu.PyinstrumentManager(interval=1e-5)
    mew.run(min_time="200x", reporter=JSONReporter(output=out), profiler_manager=mgr)

    cpu = json.loads(out.read_text())["benchmarks"][0].get("cpu_profile")
    assert cpu is not None, "sampler collected nothing; raise the iteration count"
    assert cpu["profiler"] == "pyinstrument"
    assert cpu["sample_count"] > 0
    assert "spin" in cpu["top_function"]
    # Sessions are retained only so --sample-html can render one combined report.
    assert mgr.sessions


def test_to_dict_serializes_enums_as_plain_strings(tmp_path):
    """BenchmarkResult carries strings, not bound enums: a leaked `Run.time_unit` would be
    archived as "TimeUnit.ns"."""

    @mew.benchmark(unit="us")
    def bench_units(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(min_time="1x", repetitions=2, reporter=JSONReporter(output=out))
    for bench in json.loads(out.read_text())["benchmarks"]:
        assert bench["time_unit"] == "us"
        assert bench["run_type"] in ("iteration", "aggregate")


def test_context_is_written_once_per_document(tmp_path):
    """Single-doc JSON has one context block, so rows stay bare. Only JSONL
    stamps `session`/`context` per row, where each line must stand alone."""
    mew.set_context("build", "asan")

    @mew.benchmark
    def bench_ctx(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(min_time="1x", repetitions=2, reporter=JSONReporter(output=out))
    doc = json.loads(out.read_text())
    assert doc["context"]["context"]["build"] == "asan"
    assert doc["context"]["session"]["id"]
    assert doc["benchmarks"]
    for bench in doc["benchmarks"]:
        assert "context" not in bench
        assert "session" not in bench


def test_loop_scoped_captures_carry_no_stacks(tmp_path):
    """memray drops the enclosing stack when the tracker starts in a frame that
    then returns -- exactly how a GB memory manager works. Counts stay correct
    (aggregate metadata), but records lose their frames, which is why
    `write_flamegraph` re-roots them."""
    memray = pytest.importorskip("memray")

    from mew.memory import _TRACE_PYTHON_ALLOCATORS

    def tracker_for(path):
        return memray.Tracker(path, trace_python_allocators=_TRACE_PYTHON_ALLOCATORS)

    def read_stacks(path):
        reader = memray.FileReader(path)
        stacks = [
            [f[0] for f in rec.stack_trace()]
            for rec in reader.get_high_watermark_allocation_records(merge_threads=True)
            if rec.size > 200_000
        ]
        reader.close()
        return stacks

    def start_in_helper(path, box):
        tracker = tracker_for(path)
        tracker.__enter__()
        box.append(tracker)

    # Tracker entered in the same frame as the allocation: the frame is recorded.
    same = tmp_path / "same.bin"
    tracker = tracker_for(same)
    tracker.__enter__()
    keep = bytearray(300_000)
    del keep
    tracker.__exit__(None, None, None)
    assert read_stacks(same) == [["test_loop_scoped_captures_carry_no_stacks"]]

    # Tracker entered in a helper that returns first: the stack is lost.
    nested = tmp_path / "nested.bin"
    box: list = []
    start_in_helper(nested, box)
    keep = bytearray(300_000)
    del keep
    box[0].__exit__(None, None, None)
    assert read_stacks(nested) == [[]]


def test_rooted_record_renders_with_the_real_reporter(tmp_path):
    """Pins memray's record surface: `_RootedRecord` duck-types `AllocationRecord`,
    so a release that reads a further attribute must break here, not at runtime."""
    pytest.importorskip("memray")
    from memray.reporters.flamegraph import FlameGraphReporter

    from mew.memory import _RootedRecord

    record = _RootedRecord(
        size=4096,
        n_allocations=2,
        tid=-1,
        thread_name="",
        stack=(("helper", "bench.py", 7), ("bench_demo", "bench.py", 3)),
    )
    reporter = FlameGraphReporter.from_snapshot(
        # Duck-typed stand-in, exactly as mew.memory passes them.
        cast("Any", [record]),
        memory_records=(),
        native_traces=False,
    )
    out = tmp_path / "f.html"
    with out.open("w") as f:
        reporter.render(
            f,
            metadata=_metadata_for(tmp_path),
            show_memory_leaks=False,
            merge_threads=True,
            inverted=False,
        )
    html = out.read_text()
    assert "bench_demo" in html
    assert "helper" in html


def _metadata_for(tmp_path):
    """A real `Metadata`, which only memray can construct: take one from a capture."""
    import memray

    dest = tmp_path / "meta.bin"
    with memray.Tracker(dest):
        keep = bytearray(1024)
        del keep
    with memray.FileReader(dest) as reader:
        return reader.metadata


def test_flamegraph_is_rooted_at_the_benchmark_and_loop_scoped(tmp_path):
    """The graph names each benchmark and covers the same region as the table.

    Re-rooting puts back the frame memray drops from a loop-scoped capture,
    including for body-level allocations, which otherwise carry no stack."""
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    @mew.benchmark
    def bench_rooted(state):
        fixture = bytearray(30_000_000)  # setup: excluded from the loop scope
        for _ in state:
            inline = bytearray(400_000)  # allocated directly in the body frame
            del inline
        del fixture

    out = tmp_path / "flame.html"
    with ExitStack() as stack:
        manager = _memory.manager(stack)
        mew.run(min_time="20x", reporter=None, memory_manager=manager)
        assert manager.captures, "no capture recorded"
        # Each capture is tagged with the user's benchmark frame, not a mew internal.
        assert all(root[0] == "bench_rooted" for _, root in manager.captures)
        _memory.write_flamegraph(manager, out)

    html = out.read_text()
    assert "bench_rooted" in html


def test_write_flamegraph_warns_when_nothing_was_captured(tmp_path, capsys):
    pytest.importorskip("memray")
    from contextlib import ExitStack

    from mew import memory as _memory

    out = tmp_path / "flame.html"
    with ExitStack() as stack:
        _memory.write_flamegraph(_memory.manager(stack), out)
    assert not out.exists()
    assert "no memory captures recorded" in capsys.readouterr().err


def test_pause_only_reaches_the_profiler_during_its_own_pass(tmp_path):
    """GB drives the timed run with no profiler manager, so `state.pause()` there
    must not call one: it would suspend nothing, and in a threaded run several
    worker threads would race on the manager's depth counter."""
    calls: list[str] = []

    class Probe:
        def __init__(self) -> None:
            self.sampling = False

        def after_setup_start(self) -> None:
            self.sampling = True

        def before_teardown_stop(self) -> None:
            self.sampling = False

        def get_result(self) -> None:
            return None

        def pause(self) -> None:
            calls.append("sampling" if self.sampling else "idle")

        def resume(self) -> None:
            pass

    @mew.benchmark(name="paused")
    def bench_paused(state):
        for _ in state:
            with state.pause():
                pass

    mew.run(
        min_time="20x", reporter=JSONReporter(output=tmp_path / "o.json"), profiler_manager=Probe()
    )
    assert calls, "the profiler pass must still see its pauses"
    assert set(calls) == {"sampling"}
