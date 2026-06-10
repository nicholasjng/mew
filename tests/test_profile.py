"""Profile attachment via _ProfileEnriching, plus reporter output of memory/cpu."""

from __future__ import annotations

import json

import pytest

import mew
from mew._profile import (
    EnrichedRun,
    _profile_key,
    _ProfileEnriching,
    _ProfileState,
    iter_entry_cases,
)
from mew._registry import Entry
from mew.cpu import CPUProfile
from mew.memory import MemoryProfile
from mew.reporter import JSONReporter


def _fake_mem() -> MemoryProfile:
    return MemoryProfile(profiler="memray", peak_bytes=1024, total_bytes=2048, total_allocations=5)


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


def test_enriched_run_forwards_run_fields_and_carries_profiles():
    class FakeRun:
        iterations = 7
        threads = 2
        real_accumulated_time = 0.5

        def benchmark_name(self) -> str:
            return "bench_x"

        def adjusted_real_time(self) -> float:
            return 0.25

    er = EnrichedRun(FakeRun(), memory=_fake_mem(), cpu=_fake_cpu())  # ty: ignore[invalid-argument-type]
    # Explicit forwards reach into the wrapped run.
    assert er.benchmark_name() == "bench_x"
    assert er.adjusted_real_time() == 0.25
    assert er.iterations == 7
    assert er.threads == 2
    assert er.real_accumulated_time == 0.5
    # Profile attachments are first-class fields.
    assert er.memory is not None
    assert er.memory.peak_bytes == 1024
    assert er.cpu is not None
    assert er.cpu.sample_count == 500


def test_enriched_run_handles_missing_profiles():
    class FakeRun:
        def benchmark_name(self) -> str:
            return "bench_x"

    er = EnrichedRun(FakeRun())  # ty: ignore[invalid-argument-type]
    assert er.memory is None
    assert er.cpu is None


def test_profile_enriching_attaches_by_benchmark_name():
    seen = {}

    class CapturingReporter:
        def report_context(self, ctx):
            return True

        def report_runs(self, runs):
            for r in runs:
                seen[r.benchmark_name()] = (r.memory, r.cpu)

        def finalize(self):
            pass

    class FakeName:
        def __init__(self, function_name: str, args: str = "") -> None:
            self.function_name = function_name
            self.args = args

    class FakeRun:
        def __init__(self, name: str, args: str = "") -> None:
            self._name = name
            self.run_name = FakeName(name, args)

        def benchmark_name(self) -> str:
            return self._name

    wrapped = _ProfileEnriching(
        CapturingReporter(),
        memory_profiles={"a": _fake_mem()},
        cpu_profiles={"b": _fake_cpu()},
    )
    wrapped.report_runs([FakeRun("a"), FakeRun("b"), FakeRun("c")])

    assert seen["a"][0] is not None and seen["a"][1] is None
    assert seen["b"][0] is None and seen["b"][1] is not None
    assert seen["c"] == (None, None)


def test_profile_enriching_matches_family_cases_by_structured_name():
    """A family run carries `/min_time:…` in benchmark_name() but the profile dict
    is keyed by `entry.name/case:N` — match on the structured parts, not the full name."""
    seen = {}

    class CapturingReporter:
        def report_context(self, ctx):
            return True

        def report_runs(self, runs):
            for r in runs:
                seen[r.benchmark_name()] = r.memory

        def finalize(self):
            pass

    class FakeName:
        def __init__(self, function_name: str, args: str) -> None:
            self.function_name = function_name
            self.args = args

    class FakeRun:
        def __init__(self, function_name: str, args: str, suffix: str) -> None:
            self._full = f"{function_name}/{args}{suffix}"
            self.run_name = FakeName(function_name, args)

        def benchmark_name(self) -> str:
            return self._full

    wrapped = _ProfileEnriching(
        CapturingReporter(),
        memory_profiles={"bench::f/case:0": _fake_mem(), "bench::f/case:1": _fake_mem()},
    )
    wrapped.report_runs(
        [
            FakeRun("bench::f", "case:0", "/min_time:0.200"),
            FakeRun("bench::f", "case:1", "/min_time:0.200"),
        ]
    )

    assert seen["bench::f/case:0/min_time:0.200"] is not None
    assert seen["bench::f/case:1/min_time:0.200"] is not None


def test_profile_enriching_finalize_is_optional_on_inner():
    class NoFinalize:
        def report_context(self, ctx):
            return True

        def report_runs(self, runs):
            pass

    # Should not raise.
    _ProfileEnriching(NoFinalize()).finalize()


def test_memory_profile_expands_family_keyed_per_case():
    pytest.importorskip("memray")
    from mew.memory import profile as mem_profile

    @mew.parametrize([{"n": 8}, {"n": 4_000_000}])
    def bench_alloc(state, n):
        data = bytearray(n)
        for _ in state:
            pass
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
                burn(0.02)  # excluded: must not dominate the profile
            burn(0.002)  # measured

    name = mew.REGISTRY.all()[0].name
    profiles = cpu_profile(mew.REGISTRY.all(), inner_iterations=1)
    prof = profiles[name]
    # The paused 20ms burn is ~10x the measured one; if it were sampled it would
    # dominate wall_time. Suspending the sampler keeps the profile to the ~2ms.
    assert prof.wall_time < 0.015


def test_json_reporter_emits_memory_and_cpu_blocks(tmp_path):
    name = "test::bench_explicit"

    @mew.benchmark(name=name)
    def bench_y(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    inner = JSONReporter(output=out)
    rep = _ProfileEnriching(
        inner,
        memory_profiles={name: _fake_mem()},
        cpu_profiles={name: _fake_cpu()},
    )
    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=rep)

    doc = json.loads(out.read_text())
    bench = doc["benchmarks"][0]
    assert bench["name"] == name
    assert bench["memory"] == {
        "profiler": "memray",
        "peak_bytes": 1024,
        "total_bytes": 2048,
        "total_allocations": 5,
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


def test_parquet_reporter_emits_memory_and_cpu_columns(tmp_path):
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from mew.reporter import ParquetReporter

    name = "test::bench_pq"

    @mew.benchmark(name=name)
    def bench_pq(state):
        for _ in state:
            pass

    out = tmp_path / "out.parquet"
    rep = _ProfileEnriching(
        ParquetReporter(output=out),
        memory_profiles={name: _fake_mem()},
        cpu_profiles={name: _fake_cpu()},
    )
    mew.run(argv=["mew", "--benchmark_min_time=1x"], reporter=rep)

    row = pq.read_table(out).to_pylist()[0]
    assert json.loads(row["memory"])["peak_bytes"] == 1024
    assert json.loads(row["cpu_profile"])["sample_count"] == 500


def test_parquet_reporter_nulls_profile_columns_when_absent(tmp_path):
    pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    from mew.reporter import ParquetReporter

    @mew.benchmark
    def bench_pq2(state):
        for _ in state:
            pass

    out = tmp_path / "out.parquet"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=ParquetReporter(output=out),
    )
    row = pq.read_table(out).to_pylist()[0]
    assert row["memory"] is None
    assert row["cpu_profile"] is None
