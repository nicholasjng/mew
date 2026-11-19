"""`mew report`: the archive's reduction to the page's data, and the command.

The page's JS isn't tested here; the tests check the data blob it draws.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _helpers import row, write_jsonl

from mew import _report
from mew.cli import main
from mew.config import Config
from mew.regressions import AllowRule, RegressionConfig


@pytest.fixture
def _isolated_git(monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's git config and an enclosing repo's GIT_* variables out."""
    if not shutil.which("git"):
        pytest.skip("git not available")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@t")
    monkeypatch.delenv("DYLD_INSERT_LIBRARIES", raising=False)


def _run_rows(
    n: int,
    value: float,
    *,
    commit: str = "",
    dirty: bool = False,
    host: str = "m1",
    names: tuple[str, ...] = ("b",),
    version: str | None = None,
    reps: int = 3,
) -> list[dict]:
    """The rows of run ``n``: each benchmark at ``value`` ns, ``reps`` repetitions a hair apart."""
    vcs = {"backend": "git", "commit": commit, "dirty": dirty} if commit else None
    context = {"host": host, "cpu_model": "CPU", "num_cpus": 4, "python": {"version": "3.14.0"}}
    if vcs:
        context["vcs"] = vcs
    return [
        row(
            name,
            value * (1 + rep * 1e-4),
            run_id=f"run-{host}-{n:04d}",
            date=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z",
            custom=context,
            version=version,
            repetition=rep,
            repetitions=reps,
        )
        for name in names
        for rep in range(reps)
    ]


def _history(values: list[float], **kw) -> list[dict]:
    return [r for n, v in enumerate(values) for r in _run_rows(n, v, **kw)]


def _noisy(n: int, level: float, sd: float, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    return [level * (1 + rng.gauss(0, sd)) for _ in range(n)]


def _build(tmp_path: Path, rows: list[dict], cfg: Config | None = None, **kw) -> dict:
    p = tmp_path / "history.jsonl"
    write_jsonl(p, rows)
    return _report.build(p, cfg or Config(project_root=tmp_path), **kw)


def _steps(data: dict, name: str = "b", machine: int = 0) -> list[list]:
    return data["machines"][machine]["bench"][name]["real_time"]["steps"]


# ---- steps ----------------------------------------------------------------------------------


def test_a_step_is_found_at_its_point_and_names_the_point_before(tmp_path):
    """+20% from point 15 on: one step, at 15, after 14, with the delta and p."""
    values = _noisy(15, 100, 0.005) + _noisy(15, 120, 0.005, seed=2)
    [[i, prev, delta, p, allowed]] = _steps(_build(tmp_path, _history(values)))
    assert (i, prev, allowed) == (15, 14, 0)
    assert delta == pytest.approx(0.2, abs=0.02)
    assert p < 0.01


@pytest.mark.parametrize(
    ("values", "why"),
    [
        (_noisy(15, 100, 0.005) + _noisy(15, 103, 0.005, seed=2), "under the 5% threshold"),
        (_noisy(27, 100, 0.005) + [130] * 3, "the newest points wait for a full window"),
        # Every point after past every point before, by 9.5%, but neighbours differ by 5%.
        ([100, 95] * 8 + [104, 101] * 8, "under twice the noise"),
    ],
)
def test_no_step(tmp_path, values, why):
    assert _steps(_build(tmp_path, _history(values))) == [], why


def test_changed_code_is_not_a_step(tmp_path):
    """A window spanning two versions of the benchmark doesn't count."""
    rows = [
        *(r for n in range(15) for r in _run_rows(n, 100, version="sha256:aaa")),
        *(r for n in range(15, 30) for r in _run_rows(n, 150, version="sha256:bbb")),
    ]
    data = _build(tmp_path, rows)
    assert _steps(data) == []
    assert data["versions"] == ["sha256:aaa", "sha256:bbb"]


def test_noise_is_the_robust_spread_of_neighbouring_points():
    """Neighbours 2% apart: the median difference, scaled from a MAD to a standard
    deviation and by 1/sqrt 2 for a difference of two points; too few points, no estimate."""
    usable = [True] * 40
    assert _report.noise([100.0, 102.0] * 20, [0] * 40, usable) == pytest.approx(
        1.4826 * 0.02 / 2**0.5
    )
    assert _report.noise([100.0, 102.0] * 5, [0] * 10, usable[:10]) is None


def test_band_ends_are_per_mille_of_the_median():
    assert _report._band([100.0, None, 0.0, -50.0], [99.0, 5.0, 0.0, -51.0]) == [10, None, 0, 20]


def test_allowlist_marks_a_worse_step_allowed_and_a_stricter_rule_finds_smaller_ones(tmp_path):
    rules = (
        AllowRule("lax", "known cost", 30.0),
        AllowRule("strict", "hot path", 2.0),
    )
    cfg = Config(project_root=tmp_path, regressions=RegressionConfig(rules=rules))
    up = _noisy(15, 100, 0.002) + _noisy(15, 112, 0.002, seed=2)
    small = _noisy(15, 100, 0.002) + _noisy(15, 103, 0.002, seed=2)
    rows = [
        *(r for n, v in enumerate(up) for r in _run_rows(n, v, names=("lax",))),
        *(r for n, v in enumerate(small) for r in _run_rows(n, v, names=("strict", "plain"))),
    ]
    data = _build(tmp_path, rows, cfg)
    bench = data["machines"][0]["bench"]
    assert [s[4] for s in _steps(data, "lax")] == [1]
    assert bench["lax"]["real_time"]["rule"] == [30.0, "known cost"]
    assert [s[4] for s in _steps(data, "strict")] == [0]
    assert _steps(data, "plain") == []


# ---- points ---------------------------------------------------------------------------------


def test_machines_are_separate_views_largest_first(tmp_path):
    rows = [*_history([100] * 3, host="small"), *_history([100] * 5, host="big")]
    labels = [m["label"] for m in _build(tmp_path, rows)["machines"]]
    assert labels == ["big · CPU · 4 CPUs · Python 3.14", "small · CPU · 4 CPUs · Python 3.14"]


def test_reruns_of_a_commit_pool_and_dirty_runs_stay_apart(tmp_path):
    rows = [
        *_run_rows(0, 100, commit="a" * 40),
        *_run_rows(1, 101, commit="a" * 40),
        *_run_rows(2, 150, commit="a" * 40, dirty=True),
    ]
    points = _build(tmp_path, rows)["machines"][0]["points"]
    assert [(p["k"], p["dirty"]) for p in points] == [(2, False), (1, True)]
    assert points[0]["ids"] == ["run-m1-0000", "run-m1-0001"]


@pytest.mark.usefixtures("_isolated_git")
def test_points_follow_the_history_not_the_run_dates(tmp_path):
    """A backfilled run lands at its commit; a run on a merged branch is off the history,
    and a run at a commit the repository lacks, as in a shallow clone, isn't."""

    def git(*args: str, at: int = 0) -> str:
        # A branch run goes after the history's commit made just before its own.
        env = {**os.environ, "GIT_COMMITTER_DATE": f"{1_700_000_000 + at} +0000"}
        return subprocess.run(
            ["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    main_commits = []
    for i in range(3):
        git("commit", "-q", "--allow-empty", "-m", f"main {i}", at=1000 * i)
        main_commits.append(git("rev-parse", "HEAD"))
    git("checkout", "-q", "-b", "feature", main_commits[0])
    git("commit", "-q", "--allow-empty", "-m", "on the branch", at=500)
    branch = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge feature", "feature", at=3000)
    merge = git("rev-parse", "HEAD")
    # Recorded in this order: the newest first, then a backfill, then the branch.
    rows = [
        *_run_rows(0, 100, commit=merge),
        *_run_rows(1, 100, commit=main_commits[0]),
        *_run_rows(2, 100, commit=branch),
        *_run_rows(3, 100, commit=main_commits[2]),
        *_run_rows(4, 100, commit="f" * 40),
    ]
    data = _build(tmp_path, rows, repo=_report.Repo.find(tmp_path))
    points = data["machines"][0]["points"]
    # The unknown commit goes by its run's date, the newest.
    assert [p["c"] for p in points] == [main_commits[0], branch, main_commits[2], merge, "f" * 40]
    assert [p["off"] for p in points] == [False, True, False, False, False]
    assert [s for _, s in data["chain"]] == ["main 0", "main 1", "main 2", "merge feature"]
    assert points[0]["s"] == "main 0"


@pytest.mark.parametrize(
    ("url", "commit"),
    [
        ("git@github.com:o/r.git", "https://github.com/o/r/commit/{commit}"),
        ("https://github.com/o/r", "https://github.com/o/r/commit/{commit}"),
        ("ssh://git@gitlab.com/g/sub/r.git", "https://gitlab.com/g/sub/r/-/commit/{commit}"),
        ("https://codeberg.org/o/r.git/", "https://codeberg.org/o/r/commit/{commit}"),
        ("git@bitbucket.org:o/r.git", None),
    ],
)
def test_forge_links_from_the_project_urls(tmp_path, url, commit):
    (tmp_path / "pyproject.toml").write_text(f'[project]\nurls = {{ "Source Code" = "{url}" }}\n')
    assert _report.forge_links(tmp_path, None)[0] == commit


# ---- the command ----------------------------------------------------------------------------


def _page_data(page: str) -> dict:
    blob = re.search(r'<script id="data" type="application/gzip;base64">([^<]*)</script>', page)
    assert blob
    return json.loads(gzip.decompress(base64.b64decode(blob[1])))


def test_report_writes_a_reproducible_self_contained_page(tmp_path, monkeypatch, capsys):
    values = _noisy(15, 100, 0.005) + _noisy(15, 120, 0.005, seed=2)
    write_jsonl(tmp_path / "history.jsonl.gz", _history(values))
    monkeypatch.chdir(tmp_path)
    assert main(["report", "history.jsonl.gz", "-o", "a.html"]) == 0
    assert main(["report", "history.jsonl.gz", "-o", "b.html"]) == 0
    out = capsys.readouterr().out
    assert "30 runs on 1 machine, 1 step in real time" in out
    page = (tmp_path / "a.html").read_text(encoding="utf-8")
    assert page == (tmp_path / "b.html").read_text(encoding="utf-8")
    assert "<title>history.jsonl.gz · mew report</title>" in page
    assert not re.search(r"__[A-Z_]+__", page.replace("__proto__", ""))
    assert not re.search(r"<(script|link|img)\b[^>]*\b(src|href)=", page)  # nothing to fetch
    data = _page_data(page)
    assert data["chain"] == [] and data["commitUrl"] is None
    assert len(data["machines"][0]["points"]) == 30


def test_report_where_selects_rows(tmp_path, monkeypatch):
    write_jsonl(
        tmp_path / "h.jsonl", [*_history([100] * 3, host="a"), *_history([100] * 4, host="b")]
    )
    monkeypatch.chdir(tmp_path)
    assert main(["report", "h.jsonl", "--where", "context.host=b"]) == 0
    machines = _page_data((tmp_path / "report.html").read_text(encoding="utf-8"))["machines"]
    assert [len(m["points"]) for m in machines] == [4]


@pytest.mark.usefixtures("_isolated_git")
def test_report_rejects_an_unknown_revision(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    write_jsonl(tmp_path / "h.jsonl", _history([100] * 3))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["report", "h.jsonl", "-r", "nope"])
    assert e.value.code == 2
    assert "--revision: no commit 'nope'" in capsys.readouterr().err


def test_report_names_an_unwritable_output(tmp_path, monkeypatch, capsys):
    write_jsonl(tmp_path / "h.jsonl", _history([100] * 3))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["report", "h.jsonl", "-o", "missing/r.html"])
    assert e.value.code == 2
    out = Path("missing", "r.html")
    assert capsys.readouterr().err == f"mew: error: no such file or directory: {out}\n"


@pytest.mark.usefixtures("_isolated_git")
def test_report_rejects_a_revision_outside_a_repository(tmp_path, monkeypatch, capsys):
    write_jsonl(tmp_path / "h.jsonl", _history([100]))
    monkeypatch.chdir(tmp_path)
    assert main(["report", "h.jsonl"]) == 0
    assert "1 run on 1 machine" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["report", "h.jsonl", "-r", "HEAD"])
    assert e.value.code == 2
    assert capsys.readouterr().err == "mew: error: --revision: not in a git repository\n"
