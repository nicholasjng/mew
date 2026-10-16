"""Session identity: UUIDv7 generation, VCS and machine provenance, reporter persistence."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import mew
from mew._session import new_session_id
from mew.compare import session_summaries
from mew.machine import _cpuinfo_model
from mew.reporter import JSONLReporter, JSONReporter
from mew.vcs import vcs_context


@pytest.fixture
def _isolated_vcs(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's git and jj config (signing, hooks, auto-track) and an
    enclosing repo's GIT_* variables out of the test repos."""
    # Outside tmp_path: jj would snapshot a config file in the work tree as an edit.
    jj_config = tmp_path_factory.mktemp("jj") / "config.toml"
    jj_config.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("JJ_CONFIG", str(jj_config))
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    # Commit identity, which the global config no longer provides.
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "JJ_USER"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "JJ_EMAIL"):
        monkeypatch.setenv(var, "t@t")
    # Under scripts/sanitizer-pytest.sh, git and jj would each start the sanitizer
    # runtime they don't need, about a second per process under TSAN.
    monkeypatch.delenv("DYLD_INSERT_LIBRARIES", raising=False)


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


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_outside_a_repo(tmp_path: Path):
    """Always safe to hand to `update_context`, work tree or not."""
    assert vcs_context(cwd=tmp_path) == {}


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_in_a_git_repo(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    # Without a commit, HEAD names nothing to record.
    assert vcs_context(cwd=tmp_path) == {}
    for args in (["add", "tracked.txt"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    # Untracked files are not a change to what was benchmarked: a results file or
    # build artifact in the tree must not mark every run dirty.
    (tmp_path / "results.jsonl").write_text("{}")
    info = vcs_context(cwd=tmp_path)["vcs"]
    assert info["backend"] == "git"
    assert len(info["commit"]) == 40, "full sha: an abbreviation can collide as history grows"
    assert info["branch"] == "main"
    assert info["dirty"] is False

    # A modified tracked file is.
    tracked.write_text("v2")
    assert vcs_context(cwd=tmp_path)["vcs"]["dirty"] is True

    # A detached HEAD is on no branch.
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=tmp_path, check=True)
    assert "branch" not in vcs_context(cwd=tmp_path)["vcs"]


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_prefers_jj(tmp_path: Path):
    if not shutil.which("jj"):
        pytest.skip("jj not available")
    # Colocated with one commit, git has a HEAD too: only the order picks jj.
    for args in (["git", "init", "--colocate"], ["commit", "-m", "x"]):
        subprocess.run(["jj", *args], cwd=tmp_path, capture_output=True, check=True)
    info = vcs_context(cwd=tmp_path)["vcs"]
    assert info["backend"] == "jj"
    assert info["change_id"] and info["commit"]
    assert info["dirty"] is False

    # Not yet snapshotted: only a fresh snapshot shows the edit.
    (tmp_path / "f.txt").write_text("x")
    assert vcs_context(cwd=tmp_path)["vcs"]["dirty"] is True


def test_machine_context_names_the_cpu():
    model = mew.machine_context()["cpu_model"]
    if sys.platform == "darwin":
        sysctl = ["sysctl", "-n", "machdep.cpu.brand_string"]
        assert model == subprocess.run(sysctl, capture_output=True, text=True).stdout.strip()
    elif sys.platform == "linux" and platform.machine() == "aarch64":
        pytest.skip("Arm64 kernels name no CPU model")
    else:
        # CI's x86-64 Linux and Windows runners name theirs.
        assert model and model == model.strip()


@pytest.mark.parametrize(
    ("lines", "model"),
    [
        (
            ["vendor_id\t: AuthenticAMD", "model name\t: AMD EPYC 7763 64-Core Processor"],
            "AMD EPYC 7763 64-Core Processor",
        ),
        # Arm64 kernels list implementer and part numbers instead.
        (["BogoMIPS\t: 50.00", "CPU implementer\t: 0x41", "CPU part\t: 0xd0c"], None),
    ],
)
def test_cpuinfo_model(lines: list[str], model: str | None):
    assert _cpuinfo_model(line + "\n" for line in lines) == model


def test_json_reporter_persists_session_identity(tmp_path: Path):
    """JSON rows are the same self-contained objects JSONL writes; the header
    repeats `session` / `context` for readers."""
    mew.set_context("build", "asan")

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "out.json"
    mew.run(
        min_time="1x",
        repetitions=2,
        reporter=JSONReporter(output=out),
        session_tag="before",
    )
    doc = json.loads(out.read_text())
    assert uuid.UUID(doc["session"]["id"]).version == 7
    assert doc["session"]["tag"] == "before"
    assert doc["context"]["build"] == "asan"
    # Every row, aggregates included, carries the header's blocks.
    assert doc["benchmarks"]
    for bench in doc["benchmarks"]:
        assert bench["session"] == doc["session"]
        assert bench["context"] == doc["context"]


def test_jsonl_rows_are_self_contained(tmp_path: Path):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / "out.jsonl"
    mew.run(
        min_time="1x",
        reporter=JSONLReporter(output=out),
        session_tag="before",
    )
    lines = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert all("name" in row for row in lines)  # pure NDJSON, no context line
    row = lines[0]
    assert uuid.UUID(row["session"]["id"]).version == 7
    assert row["session"]["tag"] == "before"
    assert row["session"]["host"] and row["session"]["date"]


def test_bare_reporter_context_omits_session_keys(tmp_path: Path):
    """A reporter driven without mew.run has no identity: the block is passed
    through untouched, so nothing invents a session."""
    from mew.reporter import JSONReporter

    out = tmp_path / "o.json"
    rep = JSONReporter(output=out)
    rep.report_context({"context": {"num_cpus": 4}})
    rep.finalize()
    doc = json.loads(out.read_text())
    assert "session" not in doc
    assert doc["context"] == {"num_cpus": 4}


# A gzip archive gains a new member per --append; readers see one stream.
@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_jsonl_append_makes_two_sessions(tmp_path: Path, suffix: str):
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / f"acc{suffix}"
    mew.run(
        min_time="1x",
        reporter=JSONLReporter(output=out),
        session_tag="before",
    )
    mew.run(
        min_time="1x",
        reporter=JSONLReporter(output=out, append=True),
        session_tag="after",
    )

    sessions = session_summaries(out)
    assert len(sessions) == 2
    assert {s.tag for s in sessions} == {"before", "after"}
    assert len({s.id for s in sessions}) == 2
