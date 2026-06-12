"""Variant orchestration: the _DictRun shim, CLI validation, and an end-to-end run."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from importlib.util import find_spec
from pathlib import Path

import pytest

from mew._variants import _DictRun, _pseudo_raw_context
from mew.cli import _parse_variants, _run_variants_cmd
from mew.reporter import _run_to_dict


def test_dictrun_round_trips_through_run_to_dict() -> None:
    # A child row is _run_to_dict output; wrapping it and re-serializing must
    # preserve the fields and apply the variant / repetition overrides.
    row = {
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
    run = _DictRun(row, variant="engine-a", repetition_index=3)
    out = _run_to_dict(run)
    assert out["name"] == "bench.py::f"
    assert out["real_time"] == 12.5
    assert out["time_unit"] == "ns"
    assert out["run_type"] == "iteration"
    assert out["label"] == "n=10"
    assert out["counters"] == {"items": 5.0}
    assert out["variant"] == "engine-a"
    assert out["repetition_index"] == 3  # orchestration rep, overriding the child's 0


def test_pseudo_raw_context_undoes_projection() -> None:
    child_ctx = {
        "host_name": "h",
        "num_cpus": 8,
        "cpu_scaling_enabled": False,
        "custom": {"engine": "x"},
    }
    raw = _pseudo_raw_context(child_ctx, "sid123", "before", ["a", "b"])
    assert raw["cpu_scaling"] == "disabled"  # bool -> GB string form
    assert raw["session_id"] == "sid123"
    assert raw["session_tag"] == "before"
    assert raw["variants"] == ["a", "b"]
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
            output=[],
            pattern=None,
            tags=None,
            min_time=None,
            repetitions=None,
            extra=[],
            paths=["some/path.py"],
            session_tag=None,
            append=False,
            profiling=False,
        )
    assert "mutually exclusive" in capsys.readouterr().err


def test_run_variants_rejects_profiling(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run_variants_cmd(
            ["a=x.py"],
            output=[],
            pattern=None,
            tags=None,
            min_time=None,
            repetitions=None,
            extra=[],
            paths=[],
            session_tag=None,
            append=False,
            profiling=True,
        )
    assert "profiling" in capsys.readouterr().err


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
    # 2 variants × 2 repetitions × 1 benchmark = 4 rows, all under one session.
    assert len(benches) == 4
    assert {r["variant"] for r in benches} == {"a", "b"}
    assert {r["repetition_index"] for r in benches} == {0, 1}
    ctx = next(r["context"] for r in rows if "context" in r)
    session_ids = {r.get("session_id") for r in rows if "context" in r}
    assert len(session_ids) == 1  # one shared session across children
    assert ctx["variants"] == ["a", "b"]

    # And compare can pivot it (cross-file names need --key func).
    cmp = _mew("compare", str(out), "--by", "variant", "--key", "func", cwd=tmp_path)
    assert cmp.returncode == 0, cmp.stderr
    assert "bench_work" in cmp.stdout
    assert "(baseline)" in cmp.stdout


def test_run_variant_reports_failed_child(tmp_path: Path, variant_files: tuple[Path, Path]) -> None:
    a, _ = variant_files
    missing = tmp_path / "does_not_exist.py"
    res = _mew("run", f"--variant=a={a}", f"--variant=bad={missing}", "--min-time=10x", cwd=tmp_path)
    # The good variant still ran (rows on stdout); the bad one warned; exit nonzero.
    assert res.returncode != 0
    assert "bench_work" in res.stdout
    assert "failed" in res.stderr or "No such file" in res.stderr or "no benchmarks" in res.stderr


@pytest.mark.skipif(find_spec("pyarrow") is None, reason="pyarrow not installed")
def test_run_variant_parquet_has_variant_column(
    tmp_path: Path, variant_files: tuple[Path, Path]
) -> None:
    import pyarrow.parquet as pq

    a, b = variant_files
    out = tmp_path / "r.parquet"
    res = _mew("run", f"--variant=a={a}", f"--variant=b={b}", "--min-time=10x", f"-o={out}", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    variants = {row["variant"] for row in pq.read_table(out).to_pylist()}
    assert variants == {"a", "b"}
