"""Session identity: UUIDv7 generation, VCS and machine provenance, reporter persistence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import mew
from mew.compare import read_results, session_summaries
from mew.machine import _cpuinfo_model
from mew.reporter import JSONLReporter, JSONReporter
from mew.runner import _new_session_id
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


def test_new_session_id_is_a_uuid7_led_by_the_unix_ms(monkeypatch: pytest.MonkeyPatch):
    """The 48-bit millisecond timestamp leads, so ids sort by creation time."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_750_000_000_123_456_789)
    first, second = uuid.UUID(_new_session_id()), uuid.UUID(_new_session_id())
    assert (first.version, first.variant) == (7, uuid.RFC_4122)
    assert first.int >> 80 == 1_750_000_000_123
    assert first != second  # random bits keep same-millisecond ids apart


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_outside_a_repo(tmp_path: Path):
    """Always safe to hand to `update_context`, work tree or not."""
    assert vcs_context(cwd=tmp_path) == {}


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_in_a_git_repo(tmp_path: Path):
    """In git, `vcs_context` records the full commit, branch and dirty flag once HEAD has a commit;
    untracked files are not dirty."""
    if not shutil.which("git"):
        pytest.skip("git not available")

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return proc.stdout.strip()

    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1")
    git("init", "-q", "-b", "main")
    # Without a commit, HEAD names nothing to record.
    assert vcs_context(cwd=tmp_path) == {}
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "x")
    # Untracked files are not a change to what was benchmarked: a results file or
    # build artifact in the tree must not mark every run dirty.
    (tmp_path / "results.jsonl").write_text("{}")
    # The full commit id: an abbreviation can collide as history grows.
    head = git("rev-parse", "HEAD")
    expected = {"backend": "git", "commit": head, "branch": "main", "dirty": False}
    assert vcs_context(cwd=tmp_path) == {"vcs": expected}

    # A modified tracked file is.
    tracked.write_text("v2")
    assert vcs_context(cwd=tmp_path)["vcs"]["dirty"] is True

    # A detached HEAD is on no branch.
    git("checkout", "-q", "--detach")
    assert "branch" not in vcs_context(cwd=tmp_path)["vcs"]


@pytest.mark.usefixtures("_isolated_vcs")
def test_vcs_context_prefers_jj(tmp_path: Path):
    """In a colocated jj and git repo, `vcs_context` reports jj: change id, commit and dirty flag."""
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


def test_machine_context_names_the_cpu_or_none():
    """`machine_context` gives `cpu_model` as a stripped, non-empty name or `None`."""
    # Which name the OS gives the CPU, if any, depends on the machine.
    model = mew.machine_context()["cpu_model"]
    assert model is None or model == model.strip() != ""


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
    """`_cpuinfo_model` takes the `model name` from /proc/cpuinfo lines, else `None`."""
    assert _cpuinfo_model(line + "\n" for line in lines) == model


@pytest.mark.parametrize(
    ("reporter", "suffix"), [(JSONReporter, ".json"), (JSONLReporter, ".jsonl")]
)
def test_run_stamps_session_identity_on_every_row(tmp_path: Path, reporter, suffix: str):
    """JSON and JSONL rows alike carry the run's `session` and `context` blocks."""
    mew.set_context("build", "asan")

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / f"out{suffix}"
    mew.run(min_time="1x", repetitions=2, reporter=reporter(output=out), session_tag="before")
    # read_results rejects anything but rows, such as a JSONL context line.
    first, second = read_results(out)
    session = first["session"]
    assert uuid.UUID(session["id"]).version == 7
    assert session["tag"] == "before"
    assert session["host"] and session["date"]
    assert first["context"]["build"] == "asan"
    assert (second["session"], second["context"]) == (session, first["context"])


def test_bare_reporter_context_omits_session_keys(tmp_path: Path):
    """A reporter driven without mew.run has no identity: the block is passed
    through untouched, so nothing invents a session."""
    out = tmp_path / "o.json"
    rep = JSONReporter(output=out)
    rep.report_context({"context": {"num_cpus": 4}})
    rep.finalize()
    doc = json.loads(out.read_text())
    assert "session" not in doc
    assert doc["context"] == {"num_cpus": 4}


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_jsonl_append_makes_two_sessions(tmp_path: Path, suffix: str):
    """Two runs appending to one JSONL file, plain or gzipped, record two distinct sessions."""

    # The gzip sink appends too, instead of starting the archive over.
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / f"acc{suffix}"
    mew.run(min_time="1x", reporter=JSONLReporter(output=out), session_tag="before")
    mew.run(min_time="1x", reporter=JSONLReporter(output=out, append=True), session_tag="after")

    sessions = session_summaries(out)
    assert len(sessions) == 2
    assert {s.tag for s in sessions} == {"before", "after"}
    assert len({s.id for s in sessions}) == 2
