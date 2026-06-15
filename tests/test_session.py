"""Session identity: UUIDv7 generation, git tag derivation, reporter persistence."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import mew
from mew._session import _git_describe, derive_session_tag, new_session_id
from mew.reporter import JSONLReporter, JSONReporter


def test_new_session_id_is_a_version7_uuid():
    sid = uuid.UUID(new_session_id())
    assert sid.version == 7
    assert sid.variant == uuid.RFC_4122


def test_new_session_ids_are_unique_and_time_ordered():
    first = new_session_id()
    time.sleep(0.002)  # cross a millisecond boundary so the timestamps differ
    second = new_session_id()
    assert first != second
    assert first < second  # lexicographic order matches creation order


def test_derive_session_tag_outside_a_repo(tmp_path: Path):
    assert derive_session_tag(cwd=tmp_path) is None


def test_derive_session_tag_in_a_repo(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        cwd=tmp_path,
        check=True,
    )
    tag = derive_session_tag(cwd=tmp_path)
    assert tag  # plain git repo (no jj): the short commit hash from --always


def test_derive_session_tag_prefers_jj(tmp_path: Path):
    if not shutil.which("jj"):
        pytest.skip("jj not available")
    # In a jj repo the tag comes from jj, not git. `jj git init` (not --colocate)
    # also leaves no usable git HEAD, so git is empty here regardless.
    subprocess.run(["jj", "git", "init"], cwd=tmp_path, capture_output=True, check=True)
    assert _git_describe(tmp_path) is None
    assert derive_session_tag(cwd=tmp_path)  # supplied by jj


def _run_to_jsonl(tmp_path: Path, name: str, **run_kwargs) -> dict:
    """Run the registered benchmarks into a JSONL file, return its context block."""
    out = tmp_path / f"{name}.jsonl"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONLReporter(output=out),
        **run_kwargs,
    )
    header = json.loads(out.read_text().splitlines()[0])
    return header["context"]


def test_run_stamps_session_id_into_context(tmp_path: Path):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    ctx = _run_to_jsonl(tmp_path, "a")
    assert uuid.UUID(ctx["session_id"]).version == 7
    assert "session_tag" not in ctx  # none passed, none derived at API level


def test_each_run_is_a_distinct_session(tmp_path: Path):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    first = _run_to_jsonl(tmp_path, "a")["session_id"]
    second = _run_to_jsonl(tmp_path, "b")["session_id"]
    assert first != second


def test_run_persists_session_tag(tmp_path: Path):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    ctx = _run_to_jsonl(tmp_path, "a", session_tag="before")
    assert ctx["session_tag"] == "before"


def test_json_reporter_persists_session_identity(tmp_path: Path):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONReporter(output=out),
        session_tag="before",
    )
    ctx = json.loads(out.read_text())["context"]
    assert uuid.UUID(ctx["session_id"]).version == 7
    assert ctx["session_tag"] == "before"


def test_parquet_reporter_persists_session_columns(tmp_path: Path):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    from mew.reporter import ParquetReporter

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "out.parquet"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=ParquetReporter(output=out),
        session_tag="before",
    )
    row = pq.read_table(out).to_pylist()[0]
    assert uuid.UUID(row["session_id"]).version == 7
    assert row["session_tag"] == "before"


def test_bare_reporter_context_omits_session_keys():
    """A reporter driven without mew.run (e.g. by GB directly) has no identity."""
    from mew.reporter import _build_context

    ctx = _build_context({"host_name": "h"})
    assert "session_id" not in ctx
    assert "session_tag" not in ctx


def test_jsonl_append_makes_two_sessions(tmp_path: Path):
    from mew.compare import _load_sessions

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "acc.jsonl"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONLReporter(output=out),
        session_tag="before",
    )
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=JSONLReporter(output=out, append=True),
        session_tag="after",
    )

    # Two header lines + two rows; compare splits them into two sessions, keyed
    # by their distinct session ids (not collapsed by the shared name).
    sessions = _load_sessions(out, "real_time")
    assert len(sessions) == 2
    assert {s.session_tag for s in sessions} == {"before", "after"}
    assert len({s.session_id for s in sessions}) == 2


def test_parquet_append_concatenates_sessions(tmp_path: Path):
    pytest.importorskip("pyarrow")
    from mew.compare import _load_sessions
    from mew.reporter import ParquetReporter

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "acc.parquet"
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=ParquetReporter(output=out),
        session_tag="before",
    )
    mew.run(
        argv=["mew", "--benchmark_min_time=1x"],
        reporter=ParquetReporter(output=out, append=True),
        session_tag="after",
    )

    sessions = _load_sessions(out, "real_time")
    assert len(sessions) == 2
    assert {s.session_tag for s in sessions} == {"before", "after"}


def test_cli_append_rejected_for_json(tmp_path: Path):
    from mew.cli import _build_reporters

    with pytest.raises(SystemExit):
        _build_reporters([str(tmp_path / "out.json")], append=True)
