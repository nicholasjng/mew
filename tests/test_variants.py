"""Variant orchestration: the RunRow merge, CLI validation, and an end-to-end run."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from importlib.util import find_spec
from pathlib import Path
from typing import cast

import pytest

import mew.config as _config
from mew._typing import RunRow
from mew._variants import ProfileConfig, _merge_row, _pseudo_raw_context
from mew.cli import _parse_variants, _run_variants_cmd


def _full_row(**overrides: object) -> RunRow:
    """A complete child JSONL row (a RunRow as the worker's reporters emit it)."""
    row: dict[str, object] = {
        "name": "bench.py::f",
        "run_name": "bench.py::f",
        "family_index": 0,
        "per_family_instance_index": 0,
        "run_type": "iteration",
        "aggregate_name": "",
        "repetitions": 1,
        "repetition_index": 0,
        "threads": 1,
        "iterations": 1000,
        "real_time": 12.5,
        "cpu_time": 11.0,
        "real_accumulated_time": 1.0,
        "cpu_accumulated_time": 1.0,
        "time_unit": "ns",
        "label": "n=10",
        "skipped": False,
        "skip_message": "",
        "counters": {"items": 5.0},
    }
    row.update(overrides)
    return cast("RunRow", row)


def test_merge_row_overlays_variant_and_rep() -> None:
    # A child row already is a RunRow; the merge overlays the orchestration's
    # variant and repetition index while preserving every other field.
    out = _merge_row(_full_row(), variant="engine-a", rep=3, outer_reps=5, custom=None)
    assert out["name"] == "bench.py::f"
    assert out["real_time"] == 12.5
    assert out["time_unit"] == "ns"
    assert out["counters"] == {"items": 5.0}
    assert out["variant"] == "engine-a"
    assert out["repetition_index"] == 3  # orchestration rep, overriding the child's 0
    assert out["repetitions"] == 5  # total merged repetitions
    assert "custom" not in out  # no per-suite context → no custom key


def test_merge_row_composes_inner_gb_repetitions() -> None:
    # A child that itself ran N inner GB repetitions keeps them distinct in the
    # merged output: index = outer_rep * inner_total + inner_index.
    for inner_idx in range(3):
        row = _full_row(repetitions=3, repetition_index=inner_idx)
        out = _merge_row(row, variant="a", rep=1, outer_reps=2, custom=None)
        assert out["repetition_index"] == 3 + inner_idx
        assert out["repetitions"] == 6


def test_pseudo_raw_context_undoes_projection() -> None:
    child_ctx = {
        "host_name": "h",
        "num_cpus": 8,
        "cpu_scaling_enabled": False,
        "custom": {"engine": "x"},
    }
    raw = _pseudo_raw_context(child_ctx, "sid123", "before")
    assert raw["cpu_scaling"] == "disabled"  # bool -> GB string form
    assert raw["session_id"] == "sid123"
    assert raw["session_tag"] == "before"
    assert raw["custom"] == {"engine": "x"}


def test_parse_variants_ok() -> None:
    parsed = _parse_variants(["a=x.py", "b=y.py"])
    assert parsed == {"a": Path("x.py"), "b": Path("y.py")}


@pytest.mark.parametrize("spec", ["noeq", "=onlypath.py", "name="])
def test_parse_variants_rejects_malformed(spec: str) -> None:
    with pytest.raises(SystemExit):
        _parse_variants([spec])


def test_parse_variants_rejects_duplicates(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _parse_variants(["a=x.py", "a=y.py"])
    assert "duplicate" in capsys.readouterr().err


def test_run_variants_rejects_positional_paths(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run_variants_cmd(
            ["a=x.py"],
            cfg=_config.Config(),
            output=[],
            pattern=None,
            tags=None,
            min_time=None,
            min_warmup_time=None,
            random_interleaving=False,
            repetitions=None,
            paths=["some/path.py"],
            session_tag=None,
            append=False,
        )
    assert "mutually exclusive" in capsys.readouterr().err


def test_merge_row_carries_per_variant_custom() -> None:
    # The orchestrator stamps each variant's set_context() values onto its rows
    # so the merged file records every variant's own engine, not just the first.
    out = _merge_row(
        _full_row(), variant="duckdb", rep=0, outer_reps=1, custom={"engine": "duckdb 1.5.3"}
    )
    assert out["custom"] == {"engine": "duckdb 1.5.3"}


def test_merge_row_preserves_child_memory_block() -> None:
    # A child profiled under --variant already serialized memory as a dict in its
    # RunRow; the overlay leaves it untouched (no Run to reconstruct).
    row = _full_row(
        memory={
            "profiler": "memray",
            "peak_bytes": 2048,
            "total_bytes": 1024,
            "total_allocations": 700,
            "iterations": 100,
            "allocations_per_iteration": 7.0,
        }
    )
    out = _merge_row(row, variant="a", rep=0, outer_reps=1, custom=None)
    assert out["memory"]["peak_bytes"] == 2048
    assert out["memory"]["allocations_per_iteration"] == 7.0


def test_variant_artifact_suffixes_per_variant() -> None:
    from mew._variants import _profile_args, _variant_artifact

    assert _variant_artifact(Path("out.html"), "duckdb") == Path("out.duckdb.html")
    assert _variant_artifact(None, "duckdb") is None
    args = _profile_args(ProfileConfig(profile_memory=True, flamegraph=Path("fg.html")), "ducky")
    assert "--profile-memory" in args
    assert "--flamegraph=fg.ducky.html" in args


_BENCH_A = """
    import mew
    @mew.benchmark
    def bench_work(state):
        for _ in state:
            sum(range(100))
"""
_BENCH_B = """
    import mew
    @mew.benchmark
    def bench_work(state):
        for _ in state:
            sum(range(300))
"""


def _mew(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "mew.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


@pytest.fixture
def variant_files(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "bench_a.py").write_text(textwrap.dedent(_BENCH_A))
    (tmp_path / "bench_b.py").write_text(textwrap.dedent(_BENCH_B))
    return tmp_path / "bench_a.py", tmp_path / "bench_b.py"


def test_run_variant_end_to_end(tmp_path: Path, variant_files: tuple[Path, Path]) -> None:
    a, b = variant_files
    out = tmp_path / "r.jsonl"
    res = _mew(
        "run",
        f"--variant=a={a}",
        f"--variant=b={b}",
        "--repetitions=2",
        "--min-time=10x",
        f"-o={out}",
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr

    import json

    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    benches = [r for r in rows if "name" in r]
    # Pure NDJSON: 2 variants × 2 repetitions × 1 benchmark = 4 self-contained rows.
    assert len(rows) == len(benches) == 4
    assert {r["variant"] for r in benches} == {"a", "b"}
    assert {r["repetition_index"] for r in benches} == {0, 1}
    # One shared session across children, stamped on every row.
    assert len({r["session_id"] for r in benches}) == 1

    # And compare can pivot it. --by variant defaults to --key func, so the
    # variant columns line up on the function name without an explicit --key.
    cmp = _mew("compare", str(out), "--by", "variant", cwd=tmp_path)
    assert cmp.returncode == 0, cmp.stderr
    assert "bench_work" in cmp.stdout
    assert "(baseline)" in cmp.stdout


def test_run_variant_reports_failed_child(tmp_path: Path, variant_files: tuple[Path, Path]) -> None:
    a, _ = variant_files
    missing = tmp_path / "does_not_exist.py"
    res = _mew(
        "run", f"--variant=a={a}", f"--variant=bad={missing}", "--min-time=10x", cwd=tmp_path
    )
    # The good variant still ran (rows on stdout); the bad one warned; exit nonzero.
    assert res.returncode != 0
    assert "bench_work" in res.stdout
    # Exact `_run_child` failure message, naming the actual bad file — not just
    # some indication that *something* went wrong.
    assert f"warning: variant child failed ({missing}):" in res.stderr


def test_run_variant_jsonl_has_variant_field(
    tmp_path: Path, variant_files: tuple[Path, Path]
) -> None:
    import json as _json

    a, b = variant_files
    out = tmp_path / "r.jsonl"
    res = _mew(
        "run", f"--variant=a={a}", f"--variant=b={b}", "--min-time=10x", f"-o={out}", cwd=tmp_path
    )
    assert res.returncode == 0, res.stderr
    rows = [_json.loads(ln) for ln in out.read_text().splitlines()]
    assert {row["variant"] for row in rows} == {"a", "b"}
    # Merged rows are self-contained: one shared session id stamped on all.
    assert len({row["session_id"] for row in rows}) == 1


_BENCH_CTX_A = """
    import mew
    mew.set_context("engine", "duckdb 1.5.3")
    @mew.benchmark
    def bench_work(state):
        for _ in state:
            sum(range(100))
"""
_BENCH_CTX_B = """
    import mew
    mew.set_context("engine", "ducky 0.2.0")
    @mew.benchmark
    def bench_work(state):
        for _ in state:
            sum(range(100))
"""


def test_variant_per_engine_custom_context(tmp_path: Path) -> None:
    # Each variant's own set_context() value must survive into the merged file
    # and surface as a distinct column annotation in `compare --by variant`.
    (tmp_path / "duckdb.py").write_text(textwrap.dedent(_BENCH_CTX_A))
    (tmp_path / "ducky.py").write_text(textwrap.dedent(_BENCH_CTX_B))
    out = tmp_path / "r.jsonl"
    res = _mew(
        "run",
        f"--variant=duckdb={tmp_path / 'duckdb.py'}",
        f"--variant=ducky={tmp_path / 'ducky.py'}",
        "--min-time=10x",
        f"-o={out}",
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr

    import json

    benches = [
        json.loads(line)
        for line in out.read_text().splitlines()
        if line.strip() and "name" in json.loads(line)
    ]
    by_variant = {r["variant"]: r.get("custom") for r in benches}
    assert by_variant["duckdb"] == {"engine": "duckdb 1.5.3"}
    assert by_variant["ducky"] == {"engine": "ducky 0.2.0"}

    cmp = _mew("compare", str(out), "--by", "variant", cwd=tmp_path)
    assert cmp.returncode == 0, cmp.stderr
    # Both engine strings annotate their respective columns (not just the first).
    assert "duckdb 1.5.3" in cmp.stdout
    assert "ducky 0.2.0" in cmp.stdout


@pytest.mark.skipif(find_spec("memray") is None, reason="memray not installed")
def test_variant_profile_memory_end_to_end(
    tmp_path: Path, variant_files: tuple[Path, Path]
) -> None:
    # --profile-memory now works with --variant: memory lands per variant in the
    # merged file, and compare can pivot it on a memory metric in one run.
    a, b = variant_files
    out = tmp_path / "r.jsonl"
    res = _mew(
        "run",
        f"--variant=a={a}",
        f"--variant=b={b}",
        "--profile-memory",
        "--min-time=10x",
        f"-o={out}",
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr

    import json

    benches = [
        json.loads(line)
        for line in out.read_text().splitlines()
        if line.strip() and "name" in json.loads(line)
    ]
    assert benches, "no benchmark rows emitted"
    assert all("memory" in r and r["memory"]["peak_bytes"] >= 0 for r in benches)

    cmp = _mew(
        "compare", str(out), "--by", "variant", "--metric", "memory.peak_bytes", cwd=tmp_path
    )
    assert cmp.returncode == 0, cmp.stderr
