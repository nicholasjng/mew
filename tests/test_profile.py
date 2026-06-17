"""Profile attachment via _RunProjector, plus reporter output of memory/cpu."""

from __future__ import annotations

import json
import sys

import pytest

import mew
from mew._profile import (
    _profile_key,
    _ProfileState,
    _RunProjector,
    iter_entry_cases,
)
from mew._registry import Entry
from mew.cpu import CPUProfile
from mew.memory import MemoryProfile
from mew.reporter import JSONReporter


def _proj_run(function_name: str, args: str = "", *, label: str = "", suffix: str = ""):
    """A full fake C++ Run that _run_to_dict / _RunProjector can project to a RunRow."""
    full = f"{function_name}/{args}{suffix}" if args else function_name

    class FakeName:
        def __init__(self) -> None:
            self.function_name = function_name
            self.args = args

        def __str__(self) -> str:
            return full

    class FakeEnum:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeRun:
        run_name = FakeName()
        family_index = 0
        per_family_instance_index = 0
        aggregate_name = ""
        repetitions = 1
        repetition_index = 0
        threads = 1
        iterations = 1
        real_accumulated_time = 0.0
        cpu_accumulated_time = 0.0
        report_label = label
        skipped = False
        skip_message = ""
        counters: dict = {}
        run_type = FakeEnum("iteration")
        time_unit = FakeEnum("ns")

        def benchmark_name(self) -> str:
            return full

        def adjusted_real_time(self) -> float:
            return 1.0

        def adjusted_cpu_time(self) -> float:
            return 1.0

    return FakeRun()


def _fake_mem() -> MemoryProfile:
    return MemoryProfile(
        profiler="memray",
        peak_bytes=1024,
        total_bytes=2048,
        total_allocations=5,
        iterations=1,
        allocations_per_iteration=5.0,
    )


def _fake_cpu() -> CPUProfile:
    return CPUProfile(
        profiler="pyinstrument",
        wall_time=0.5,
        sample_count=500,
        top_function="foo (bar.py:1)",
        top_function_total_self_time=0.3,
    )


def test_profilestate_runs_n_times():
    seen = sum(1 for _ in _ProfileState(n_iterations=3))
    assert seen == 3


def test_profilestate_default_runs_once():
    seen = sum(1 for _ in _ProfileState())
    assert seen == 1


def test_profilestate_exposes_iteration_count_via_properties():
    state = _ProfileState(n_iterations=42)
    assert state.iterations == 42
    assert state.max_iterations == 42


def test_profilestate_range_returns_configured_value():
    assert _ProfileState().range(0) == 0
    assert _ProfileState(range_value=3).range(0) == 3


def test_profile_key_omits_empty_args():
    assert _profile_key("file.py::bench", "") == "file.py::bench"
    assert _profile_key("file.py::bench", "case:2") == "file.py::bench/case:2"


def _entry(name, *, case_labels=None):
    return Entry(name=name, fn=lambda state: None, case_labels=case_labels)


def test_iter_entry_cases_single_benchmark():
    assert list(iter_entry_cases(_entry("f::bench"))) == [("f::bench", 0)]


def test_iter_entry_cases_expands_family_with_range_indices():
    cases = list(iter_entry_cases(_entry("f::bench", case_labels=["n=1", "n=2", "n=3"])))
    assert cases == [
        ("f::bench/case:0", 0),
        ("f::bench/case:1", 1),
        ("f::bench/case:2", 2),
    ]


def test_iter_entry_cases_drives_distinct_cases():
    # Regression for: only case 0 ever ran because range() returned 0.
    runs = []

    @mew.parametrize([{"n": 10}, {"n": 20}, {"n": 30}])
    def bench_fam(state, n):
        runs.append(n)
        for _ in state:
            pass

    entry = next(e for e in mew.REGISTRY.all() if e.case_labels is not None)
    for _, rng in iter_entry_cases(entry):
        entry.fn(_ProfileState(range_value=rng))
    assert runs == [10, 20, 30]


def test_run_projector_attaches_profiles_to_rows_by_name():
    seen = {}

    class CapturingReporter:
        def report_context(self, ctx):
            return True

        def report_runs(self, rows):
            # The projector emits RunRow dicts; profiles are dict keys, absent
            # when no profile was attached for that case.
            for row in rows:
                seen[row["name"]] = (row.get("memory"), row.get("cpu_profile"))

        def finalize(self):
            pass

    wrapped = _RunProjector(
        CapturingReporter(),
        memory_profiles={"a": _fake_mem()},
        cpu_profiles={"b": _fake_cpu()},
    )
    wrapped.report_runs([_proj_run("a"), _proj_run("b"), _proj_run("c")])

    assert seen["a"][0] is not None and seen["a"][1] is None
    assert seen["b"][0] is None and seen["b"][1] is not None
    assert seen["c"] == (None, None)


def test_run_projector_matches_family_cases_by_structured_name():
    """A family run carries `/min_time:…` in benchmark_name() but the profile dict
    is keyed by `entry.name/case:N` — match on the structured parts, not the full name."""
    seen = {}

    class CapturingReporter:
        def report_context(self, ctx):
            return True

        def report_runs(self, rows):
            for row in rows:
                seen[row["name"]] = row.get("memory")

        def finalize(self):
            pass

    wrapped = _RunProjector(
        CapturingReporter(),
        memory_profiles={"bench::f/case:0": _fake_mem(), "bench::f/case:1": _fake_mem()},
    )
    wrapped.report_runs(
        [
            _proj_run("bench::f", "case:0", suffix="/min_time:0.200"),
            _proj_run("bench::f", "case:1", suffix="/min_time:0.200"),
        ]
    )

    assert seen["bench::f/case:0/min_time:0.200"] is not None
    assert seen["bench::f/case:1/min_time:0.200"] is not None


def test_run_projector_finalize_is_optional_on_inner():
    class NoFinalize:
        def report_context(self, ctx):
            return True

        def report_runs(self, rows):
            pass

    # Should not raise.
    _RunProjector(NoFinalize()).finalize()


@pytest.mark.skipif(
    not getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="memray does not track allocations on a free-threaded interpreter "
    "(returns empty profiles), so the per-case byte comparison is meaningless",
)
def test_memory_profile_expands_family_keyed_per_case():
    pytest.importorskip("memray")
    from mew.memory import profile as mem_profile

    @mew.parametrize([{"n": 8}, {"n": 4_000_000}])
    def bench_alloc(state, n):
        data = None
        for _ in state:
            data = bytearray(n)
        del data

    entries = mew.REGISTRY.all()
    name = entries[0].name
    profiles = mem_profile(entries)

    # Both cases get their own profile (regression for #1/#2: previously only
    # `entry.name` was keyed, and only case 0 ever ran).
    assert set(profiles) == {f"{name}/case:0", f"{name}/case:1"}
    # Driving range_value per case actually changes behavior: case 1 holds a
    # multi-MB buffer live at the high-water mark, case 0 a few bytes.
    assert profiles[f"{name}/case:1"].total_bytes > profiles[f"{name}/case:0"].total_bytes


def test_memory_profile_excludes_setup_allocations():
    """The capture is scoped to the timing loop: a large fixture allocated before
    the loop must not appear in the tracked stats."""
    pytest.importorskip("memray")
    from mew.memory import profile as mem_profile

    setup_bytes = 50_000_000

    @mew.benchmark
    def bench_setup_heavy(state):
        fixture = bytearray(setup_bytes)  # setup: outside the capture window
        data = None
        for _ in state:
            data = bytearray(10_000)
        del fixture, data

    name = mew.REGISTRY.all()[0].name
    prof = mem_profile(mew.REGISTRY.all())[name]
    # Tracked high-water-mark bytes reflect the loop's ~10 KB, not the 50 MB fixture.
    assert prof.total_bytes < setup_bytes / 10
    # The 50 MB fixture is one big allocation outside the loop; per measured
    # iteration the body makes only a handful (iteration-count-independent).
    assert prof.iterations == 100
    assert prof.allocations_per_iteration < 50
    assert prof.allocations_per_iteration == prof.total_allocations / prof.iterations


def test_memory_profile_skips_body_that_never_iterates(capsys):
    pytest.importorskip("memray")
    from mew.memory import profile as mem_profile

    @mew.benchmark
    def bench_no_loop(state):
        pass

    profiles = mem_profile(mew.REGISTRY.all())
    assert profiles == {}
    assert "never iterated" in capsys.readouterr().err


def test_memory_profile_closes_tracker_when_body_raises(tmp_path):
    pytest.importorskip("memray")
    from mew.memory import _capture_case

    def exploding(state):
        for _ in state:
            raise RuntimeError("boom")

    # warmup=0 forces the raise into the tracked pass, exercising the finally
    # that closes the tracker (a leaked global tracker would break the next case).
    with pytest.raises(RuntimeError, match="boom"):
        _capture_case(exploding, 0, tmp_path / "boom.bin", iterations=5, warmup=0)

    # The tracker was closed on the way out: a fresh tracker can start.
    def drain(state):
        for _ in state:
            pass

    assert _capture_case(drain, 0, tmp_path / "next.bin", iterations=5, warmup=1)


def test_profilestate_loop_hooks_fire_once_around_the_loop():
    events = []
    state = _ProfileState(
        n_iterations=3,
        on_loop_start=lambda: events.append("start"),
        on_loop_end=lambda: events.append("end"),
    )
    for _ in state:
        events.append("iter")
    assert events == ["start", "iter", "iter", "iter", "end"]


def test_profilestate_loop_hooks_fire_for_batches():
    events = []
    state = _ProfileState(
        n_iterations=4,
        on_loop_start=lambda: events.append("start"),
        on_loop_end=lambda: events.append("end"),
    )
    for n in state.batches(2):
        events.append(f"batch:{n}")
    assert events == ["start", "batch:2", "batch:2", "end"]


def test_cpu_profile_expands_family_keyed_per_case():
    pytest.importorskip("pyinstrument")
    from mew.cpu import profile as cpu_profile

    @mew.parametrize([{"n": 1}, {"n": 2}])
    def bench_cpu(state, n):
        for _ in state:
            sum(range(100 * n))

    entries = mew.REGISTRY.all()
    name = entries[0].name
    profiles = cpu_profile(entries, inner_iterations=50)
    assert set(profiles) == {f"{name}/case:0", f"{name}/case:1"}


def test_cpu_profile_html_reuses_stats_pass(tmp_path):
    # The HTML report renders from the sessions the stats pass already
    # captured; requesting it must not re-execute the suite.
    pytest.importorskip("pyinstrument")
    from mew.cpu import profile as cpu_profile

    calls: list[int] = []

    @mew.benchmark
    def bench_counted(state):
        calls.append(1)
        for _ in state:
            sum(range(100))

    out = tmp_path / "report.html"
    profiles = cpu_profile(mew.REGISTRY.all(), output=out, inner_iterations=10)
    assert profiles  # stats still collected
    assert out.is_file() and out.stat().st_size > 0
    assert len(calls) == 1  # one execution, not one per output


def test_cpu_profile_excludes_paused_regions():
    pytest.importorskip("pyinstrument")
    import time

    from mew.cpu import profile as cpu_profile

    def burn(seconds: float) -> None:
        end = time.process_time() + seconds
        while time.process_time() < end:
            pass

    @mew.benchmark
    def bench_paused(state):
        for _ in state:
            with state.pause():
                burn(0.2)  # excluded; ~100x the measured burn
            burn(0.002)  # measured

    name = mew.REGISTRY.all()[0].name
    profiles = cpu_profile(mew.REGISTRY.all(), inner_iterations=1)
    prof = profiles[name]
    # If the pause didn't suspend sampling, wall_time would include the 200ms burn.
    # Threshold sits well below 200ms but well above Windows' ~15.6ms process_time
    # granularity (which inflates the "2ms" measured burn to one clock tick).
    assert prof.wall_time < 0.1


def test_json_reporter_emits_memory_and_cpu_blocks(tmp_path):
    name = "test::bench_explicit"

    @mew.benchmark(name=name)
    def bench_y(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONReporter(output=out),
        memory_profiles={name: _fake_mem()},
        cpu_profiles={name: _fake_cpu()},
    )

    doc = json.loads(out.read_text())
    bench = doc["benchmarks"][0]
    assert bench["name"] == name
    assert bench["memory"] == {
        "profiler": "memray",
        "peak_bytes": 1024,
        "total_bytes": 2048,
        "total_allocations": 5,
        "iterations": 1,
        "allocations_per_iteration": 5.0,
    }
    assert bench["cpu_profile"] == {
        "profiler": "pyinstrument",
        "wall_time": 0.5,
        "sample_count": 500,
        "top_function": "foo (bar.py:1)",
        "top_function_total_self_time": 0.3,
    }


def test_json_reporter_omits_memory_and_cpu_when_absent(tmp_path):
    @mew.benchmark
    def bench_z(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONReporter(output=out),
    )
    bench = json.loads(out.read_text())["benchmarks"][0]
    assert "memory" not in bench
    assert "cpu_profile" not in bench


def test_jsonl_reporter_emits_memory_and_cpu_blocks(tmp_path):
    from mew.reporter import JSONLReporter

    name = "test::bench_jl"

    @mew.benchmark(name=name)
    def bench_jl(state):
        for _ in state:
            pass

    out = tmp_path / "out.jsonl"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONLReporter(output=out),
        memory_profiles={name: _fake_mem()},
        cpu_profiles={name: _fake_cpu()},
    )

    row = json.loads(out.read_text().splitlines()[0])
    assert row["memory"]["peak_bytes"] == 1024
    assert row["cpu_profile"]["sample_count"] == 500


def test_jsonl_reporter_omits_profile_blocks_when_absent(tmp_path):
    from mew.reporter import JSONLReporter

    @mew.benchmark
    def bench_jl2(state):
        for _ in state:
            pass

    out = tmp_path / "out.jsonl"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONLReporter(output=out),
    )
    row = json.loads(out.read_text().splitlines()[0])
    assert "memory" not in row
    assert "cpu_profile" not in row
