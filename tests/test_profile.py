"""Profile attachment via _ProfileEnriching, plus reporter output of memory/cpu."""

from __future__ import annotations

import json

import pytest

import mew
from mew._profile import EnrichedRun, _MockState, _ProfileEnriching
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


def test_mockstate_runs_n_times():
    seen = sum(1 for _ in _MockState(n_iterations=3))
    assert seen == 3


def test_mockstate_default_runs_once():
    seen = sum(1 for _ in _MockState())
    assert seen == 1


def test_mockstate_exposes_iteration_count_via_properties():
    state = _MockState(n_iterations=42)
    assert state.iterations == 42
    assert state.max_iterations == 42


def test_enriched_run_proxies_attributes_and_carries_profiles():
    class FakeRun:
        x = 42

        def benchmark_name(self) -> str:
            return "bench_x"

    er = EnrichedRun(FakeRun(), memory=_fake_mem(), cpu=_fake_cpu())
    # Proxy reaches into the wrapped run.
    assert er.x == 42
    assert er.benchmark_name() == "bench_x"
    # Attachments are first-class slots, not proxied.
    assert er.memory.peak_bytes == 1024
    assert er.cpu.sample_count == 500


def test_enriched_run_handles_missing_profiles():
    class FakeRun:
        def benchmark_name(self) -> str:
            return "bench_x"

    er = EnrichedRun(FakeRun())
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

    class FakeRun:
        def __init__(self, name: str) -> None:
            self._name = name

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


def test_profile_enriching_finalize_is_optional_on_inner():
    class NoFinalize:
        def report_context(self, ctx):
            return True

        def report_runs(self, runs):
            pass

    # Should not raise.
    _ProfileEnriching(NoFinalize()).finalize()


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
