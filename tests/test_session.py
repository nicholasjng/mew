"""Session identity: UUIDv7 generation, VCS and machine provenance, reporter persistence."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
import uuid
from pathlib import Path

import pytest

import mew
from mew import runner
from mew.compare import read_results, run_summaries
from mew.machine import _cpuinfo_model, vcs_context
from mew.reporter import JSONLReporter, JSONReporter
from mew.runner import _new_run_id


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


def test_new_run_id_is_a_uuid7_led_by_the_unix_ms(monkeypatch: pytest.MonkeyPatch):
    """The 48-bit millisecond timestamp leads, so ids sort by creation time."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_750_000_000_123_456_789)
    monkeypatch.setattr(runner, "_last_run_id", 0)  # forget earlier tests' ids
    first, second = uuid.UUID(_new_run_id()), uuid.UUID(_new_run_id())
    assert (first.version, first.variant) == (7, uuid.RFC_4122)
    assert first.int >> 80 == 1_750_000_000_123
    # On the same clock reading, the second id still sorts after the first.
    assert second.version == 7
    assert str(first) < str(second)


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
    """`machine_context` gives `cpu_model` as a stripped, non-empty name, which macOS, Windows
    and Linux on x86 always provide; only Linux on Arm may leave it `None`."""
    model = mew.machine_context()["cpu_model"]
    arm_linux = sys.platform == "linux" and platform.machine() in ("aarch64", "arm64")
    if model is None:
        assert arm_linux, "the OS names this CPU, but mew found no name"
    else:
        assert model == model.strip() != ""


def test_machine_context_tells_a_free_threaded_build_with_the_gil_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """A free-threaded build stays identifiable when an extension enabled the GIL at runtime."""
    original_get_config_var = sysconfig.get_config_var

    def get_config_var(key: str) -> int | str | None:
        return 1 if key == "Py_GIL_DISABLED" else original_get_config_var(key)

    monkeypatch.setattr(sysconfig, "get_config_var", get_config_var)
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    python = mew.machine_context()["python"]
    assert (python["free_threaded_build"], python["gil_enabled"]) == (True, True)


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
def test_run_stamps_its_identity_on_every_row(tmp_path: Path, reporter, suffix: str):
    """JSON and JSONL rows alike carry the run's `run` and `context` blocks."""

    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / f"out{suffix}"
    rep = reporter(output=out)
    mew.run(
        min_time="1x", repetitions=2, reporter=rep, run_name="before", context={"build": "asan"}
    )
    # read_results rejects anything but rows, such as a JSONL context line.
    first, second = read_results(out)
    run = first["run"]
    assert uuid.UUID(run["id"]).version == 7
    assert run["name"] == "before"
    assert run["date"].endswith("Z")
    # The host name often names a person, so mew doesn't record it.
    assert "host" not in run and "host" not in first["context"]
    assert first["context"]["build"] == "asan"
    assert (second["run"], second["context"]) == (run, first["context"])


@pytest.mark.parametrize("suffix", [".jsonl", ".jsonl.gz"])
def test_jsonl_append_keeps_both_runs(tmp_path: Path, suffix: str):
    """Two runs appending to one JSONL file, plain or gzipped, stay two distinct runs."""

    # The gzip sink appends too, instead of starting the archive over.
    @mew.benchmark
    def bench_s(state):
        for _ in state:
            pass

    out = tmp_path / f"acc{suffix}"
    mew.run(min_time="1x", reporter=JSONLReporter(output=out), run_name="before")
    mew.run(min_time="1x", reporter=JSONLReporter(output=out, append=True), run_name="after")

    runs = run_summaries(out)
    assert [r.name for r in runs] == ["after", "before"]  # newest first
    assert len({r.id for r in runs}) == 2


def test_git_dirty_state_is_unknown_when_status_fails(monkeypatch):
    """A failed `git status`, e.g. a timeout, leaves `dirty` out rather than recording clean.
    Clean and dirty trees are covered with a real repository above."""
    from mew import machine as vcs

    def fake_run(cwd, program, *args):
        if program == "jj":
            return None
        return "abc123\nmain" if args[0] == "rev-parse" else None  # status failed

    monkeypatch.setattr(vcs, "_run", fake_run)
    assert vcs.vcs_context()["vcs"] == {"backend": "git", "commit": "abc123", "branch": "main"}
