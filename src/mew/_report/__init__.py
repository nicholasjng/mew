"""``mew report``: a self-contained HTML page of a result archive's history.

Python reduces the archive to one JSON blob: per machine, metric, benchmark and
point, the median of the repetitions and their range, plus the steps, each
benchmark's noise and a suite index. The page draws it with uPlot, inlined
(MIT, ``licenses/uplot.txt``), and holds the blob gzipped.

A point is the clean runs of one commit, pooled, or one dirty or commit-less run.
In a repository, points follow the first-parent history of a revision rather
than run dates, so runs backfilled at old commits land where their code belongs,
and a step names the commits between the points around it.
"""

from __future__ import annotations

import base64
import bisect
import gzip
import html
import itertools
import json
import math
import re
import shutil
import statistics
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast

from mew._paths import parse_where
from mew._results import MEMORY_METRICS, load_runs, mannwhitney_p
from mew.config import Config
from mew.regressions import CounterConfig, RegressionConfig, Verdict

MEMORY_LABELS = {
    "peak_bytes": ("Peak memory", "bytes"),
    "allocations_per_iteration": ("Allocations per iteration", "count"),
}
MEMORY_FIELDS = sorted(m.partition(".")[2] for m in MEMORY_METRICS)

# A step: the medians of WINDOW points each side differ by the threshold and
# NOISE_K times the noise, with every point past every other (exact Mann-Whitney
# p < ALPHA). Below 5 points a side p < 0.01 can't happen: 3 against 3 is at best
# p = 0.1. Medians of 5 differ with a standard error of about 0.76 noise, so 2x the
# noise is about 2.6 standard errors; 3x would ask 6% of a typical +-2% CI runner.
WINDOW, ALPHA, NOISE_K = 5, 0.01, 2


def _finite(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _values(row: Mapping[str, Any]) -> Iterator[tuple[str, Any]]:
    """A row's metrics as ``(key, value)``, only those it has: faster than a lookup per metric."""
    yield "real_time", row.get("real_time")
    yield "cpu_time", row.get("cpu_time")
    for c in row.get("counters") or ():
        if isinstance(c, dict):
            yield f"counters.{c.get('name')}", c.get("value")
    if isinstance(mem := row.get("memory"), dict):
        for f in MEMORY_FIELDS:
            yield f"memory.{f}", mem.get(f)


def _band(meds: list[float | None], ends: list[float | None]) -> list[int | None]:
    """Each band end as per-mille of its median away from it, which gzips far better
    than the absolute value. The page turns it back."""
    return [
        None if m is None or e is None else (round(abs(e - m) / abs(m) * 1000) if m else 0)
        for m, e in zip(meds, ends, strict=True)
    ]


def _sig(v: float | None) -> float | None:
    """``v`` to 4 significant digits, which keeps the blob small."""
    return None if v is None else float(f"{v:.4g}")


def machine_label(ctx: Mapping[str, Any]) -> str:
    """Machine and interpreter: the context results shouldn't be pooled across."""
    py = ctx.get("python") or {}
    impl = py.get("implementation")
    impl = {"cpython": "CPython", "pypy": "PyPy"}.get(impl, impl or "Python")
    minor = ".".join(str(py.get("version", "")).split(".")[:2])
    ft = " free-threaded" if py.get("free_threaded_build") else ""
    parts = [
        ctx.get("host"),
        ctx.get("cpu_model") or "unknown CPU",
        f"{ctx['num_cpus']} CPUs" if ctx.get("num_cpus") else None,
        f"{impl} {minor}{ft}",
    ]
    return " · ".join(str(p) for p in parts if p)


def _metric_meta(key: str, formats: Mapping[str, Any], counters: CounterConfig) -> dict[str, Any]:
    if key in ("real_time", "cpu_time"):
        label = "Real time" if key == "real_time" else "CPU time"
        return {"key": key, "label": label, "kind": "time", "dir": "lower"}
    head, _, tail = key.partition(".")
    if head == "memory":
        label, kind = MEMORY_LABELS.get(tail, (tail, "count"))
        return {"key": key, "label": label, "kind": kind, "dir": "lower"}
    rate = bool((formats.get(tail) or {}).get("rate"))
    kind = ("bytes_rate" if rate else "bytes") if "bytes" in tail else ("rate" if rate else "count")
    d = counters.direction(tail)
    return {
        "key": key,
        "label": tail,
        "kind": kind,
        "dir": None if d is None else ("higher" if d else "lower"),
    }


# ---- the repository: subjects, the history, rewritten commits ------------------------------


class Repo:
    """Read-only questions to git (and jj, for rewritten commits) about a project."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.jj = shutil.which("jj") is not None and self._run("jj", "root") is not None

    @classmethod
    def find(cls, path: Path) -> Repo | None:
        """The repository ``path`` is in, or ``None`` outside one, or without git."""
        repo = cls(path)
        return repo if repo._run("git", "rev-parse", "--git-dir") is not None else None

    def _run(self, *args: str, stdin: str | None = None) -> str | None:
        try:
            proc = subprocess.run(
                args, cwd=self.path, input=stdin, capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def has_revision(self, rev: str) -> bool:
        return self._run("git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}") is not None

    def remote_url(self) -> str | None:
        return (self._run("git", "remote", "get-url", "origin") or "").strip() or None

    def existing(self, commits: set[str]) -> set[str]:
        """The commits the repository has: a shallow clone or a rebase loses some."""
        stdin = "\n".join(commits) + "\n"
        out = self._run("git", "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=stdin)
        return {line.split()[0] for line in (out or "").splitlines() if line.endswith(" commit")}

    def by_change_id(self, change_ids: set[str]) -> dict[str, str]:
        """jj only: the current commit of each change, which survives rewrites."""
        if not self.jj or not change_ids:
            return {}
        revset = " | ".join(f"present(change_id({c}))" for c in sorted(change_ids))
        template = 'change_id.short(12) ++ " " ++ commit_id ++ "\\n"'
        out = self._run(
            "jj", "log", "--no-graph", "--ignore-working-copy", "-r", revset, "-T", template
        )
        return dict(line.split() for line in (out or "").splitlines() if len(line.split()) == 2)

    def describe(self, commits: set[str]) -> dict[str, tuple[int, str]]:
        """Commit time and subject of each commit, in one process. Each must exist."""
        if not commits:
            return {}
        fmt = "--format=%H%x00%ct%x00%s"
        out = self._run(
            "git", "log", "--no-walk=unsorted", "--stdin", fmt, stdin="\n".join(commits) + "\n"
        )
        info = {}
        for line in (out or "").splitlines():
            h, t, s = line.split("\0", 2)
            info[h] = (int(t), s)
        return info

    def history(self, rev: str, since: int) -> list[tuple[str, int, str]]:
        """The first-parent chain from ``rev``, oldest first, back to a day before ``since``.

        First-parent keeps a merged branch as one commit, the merge, which is how a
        main branch reads; a run on the branch itself is off this history.
        """
        fmt = "--format=%H%x00%ct%x00%s"
        out = self._run("git", "log", "--first-parent", f"--since={since - 86400}", fmt, rev)
        chain = []
        for line in (out or "").splitlines():
            h, t, s = line.split("\0", 2)
            chain.append((h, int(t), s))
        return chain[::-1]


# The forges whose commit and compare URLs mew knows, by host.
_GITHUB_STYLE = ("/commit/{commit}", "/compare/{base}...{head}")
_GITLAB_STYLE = ("/-/commit/{commit}", "/-/compare/{base}...{head}")


def forge_links(root: Path, repo: Repo | None) -> tuple[str | None, str | None]:
    """Commit and compare URL templates for the project's forge, if it's a known one.

    The repository URL is ``[project.urls]``'s ``Source`` or ``Repository`` entry,
    labels normalized as PEP 753 says, else the ``origin`` remote.
    """
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get(
            "project"
        )
        urls = project.get("urls") if isinstance(project, dict) else None
    except (OSError, ValueError):
        urls = None
    if not isinstance(urls, dict):
        urls = {}
    norm = {re.sub(r"[^a-z]", "", str(k).lower()): v for k, v in urls.items() if isinstance(v, str)}
    url = next((norm[k] for k in ("source", "sourcecode", "repository", "code") if k in norm), None)
    if url is None and repo is not None:
        url = repo.remote_url()
    # git@host:o/r.git, ssh://git@host/o/r.git, https://host/o/r(.git) -> host, o/r
    m = re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?$", url or "")
    if not m:
        return None, None
    host, path = m.groups()
    if host in ("github.com", "codeberg.org") or "gitea" in host or "forgejo" in host:
        style = _GITHUB_STYLE
    elif "gitlab" in host:
        style = _GITLAB_STYLE
    else:
        return None, None
    base = f"https://{host}/{path}"
    return base + style[0], base + style[1]


# ---- points: runs placed on the history, reruns of a commit pooled ------------------------


@dataclass
class _Run:
    """What placing a run needs: its commit as recorded and as the repository knows it now."""

    recorded: str
    change_id: str | None
    dirty: bool
    date: str
    ts: int
    name: str
    id: str
    commit: str = ""
    subject: str = ""
    rewritten: bool = False
    ci: int | None = None  # index in the history, None when off it or unknown
    off: bool = False  # a commit the repository has, but not in the history
    key: tuple = ()


@dataclass
class _Point:
    runs: list[int]
    commit: str
    subject: str
    dirty: bool
    off: bool
    ci: int | None
    key: tuple
    names: list[str] = field(default_factory=list)


def _timestamp(date: str) -> int:
    try:
        return int(datetime.fromisoformat(date).timestamp())
    except ValueError:
        return 0


def _place(
    runs: list[tuple[list[dict[str, Any]], dict[str, Any]]], repo: Repo | None, rev: str
) -> tuple[list[_Run], list[list[str]], int]:
    """Each run's commit, subject and order key.

    Returns the runs, the slice of ``rev``'s history they span ([hash, subject],
    oldest first) and the history index of that slice's first commit.
    """
    infos = []
    for _, prov in runs:
        vcs = (prov.get("context") or {}).get("vcs") or {}
        run = prov.get("run") or {}
        date = run.get("date") or ""
        infos.append(
            _Run(
                recorded=vcs.get("commit") or "",
                change_id=vcs.get("change_id"),
                dirty=bool(vcs.get("dirty")),
                date=date,
                ts=_timestamp(date),
                name=run.get("name") or "",
                id=run.get("id") or "",
            )
        )
    if repo is None:
        for n, info in enumerate(infos):
            info.commit, info.key = info.recorded, (info.ts, n)
        return infos, [], 0

    recorded = {i.recorded for i in infos if i.recorded}
    have = repo.existing(recorded)
    lost = {i.change_id for i in infos if i.recorded and i.recorded not in have and i.change_id}
    moved = repo.by_change_id(lost)
    for info in infos:
        c = info.recorded
        info.commit = c if c in have else moved.get(info.change_id or "", c)
        info.rewritten = info.commit != c
    # git log fails as a whole on one commit it lacks.
    desc = repo.describe({i.commit for i in infos if i.commit in have or i.rewritten})
    since = min((t for t, _ in desc.values()), default=min(i.ts for i in infos))
    chain = repo.history(rev, since)
    index = {h: k for k, (h, _, _) in enumerate(chain)}
    times = [t for _, t, _ in chain]
    for n, info in enumerate(infos):
        info.subject = desc.get(info.commit, (0, ""))[1]
        info.ci = index.get(info.commit)
        # Unknown to the repository, as in a shallow clone, isn't off the history.
        info.off = info.ci is None and info.commit in desc
        if info.ci is not None:
            # In the history: by position there, reruns of a commit by date.
            info.key = (info.ci, 0, info.ts, n)
        else:
            # Off it (a branch) or unknown: after the history's commit made just before it.
            t = desc[info.commit][0] if info.commit in desc else info.ts
            info.key = (bisect.bisect_right(times, t) - 1, 1, info.ts, n)
    cis = [i.ci for i in infos if i.ci is not None]
    lo, hi = (min(cis), max(cis)) if cis else (0, -1)
    return infos, [[h, s] for h, _, s in chain[lo : hi + 1]], lo


def _points(members: list[int], infos: list[_Run]) -> list[_Point]:
    """Order a machine's runs, pooling the clean runs of one commit into one point.

    Reruns of a commit measure the same code, and the x axis is commits; the
    point's min-max band still shows how far the reruns spread. A dirty run is
    its own code, so it stays its own point.
    """
    pts: dict[tuple, _Point] = {}
    for g in sorted(members, key=lambda g: infos[g].key):
        info = infos[g]
        k = ("c", info.commit) if info.commit and not info.dirty else ("r", g)
        if k not in pts:
            pts[k] = _Point([], info.commit, info.subject, info.dirty, info.off, info.ci, info.key)
        pts[k].runs.append(g)
        if info.name and info.name not in pts[k].names:
            pts[k].names.append(info.name)
    return sorted(pts.values(), key=lambda p: p.key)


# ---- noise, steps and the suite index ------------------------------------------------------


def noise(meds: list[float | None], vers: list[Any], usable: list[bool]) -> float | None:
    """The relative standard deviation of a benchmark's point medians, robustly.

    From the differences between neighbouring usable points of one version: a
    step or a drift moves few of them, so their median absolute value still
    measures the noise (MAD scaled to a standard deviation, over sqrt 2, since a
    difference holds two points' noise). ``None`` with too few points to say.
    """
    vals = {i: m for i, m in enumerate(meds) if m and usable[i]}
    diffs = [abs(vals[j] / vals[i] - 1) for i, j in itertools.pairwise(vals) if vers[i] == vers[j]]
    if len(diffs) < 2 * WINDOW:
        return None
    return 1.4826 * statistics.median(diffs) / math.sqrt(2)


def find_steps(
    meds: list[float | None],
    vers: list[Any],
    usable: list[bool],
    threshold: float,
    sd: float | None,
) -> list[list[Any]]:
    """Steps between the ``WINDOW`` points before each point and the ``WINDOW`` from it on.

    The test runs on point medians, not repetitions: a run's repetitions share its
    noise (load, thermal state), so pooling them overstates significance. Both
    windows must be full and within one version, so the newest points wait for
    confirmation. Dirty points and points off the history don't take part. A step
    must clear ``threshold`` and ``NOISE_K`` times the noise ``sd``.

    Returns ``[point, previous usable point, delta, p]`` per step: the step lies
    in the commits after the previous point, up to the point.
    """
    least = max(threshold, NOISE_K * (sd or 0))
    pts = [i for i, m in enumerate(meds) if m is not None and usable[i]]
    cands = []
    for k in range(WINDOW, len(pts) - WINDOW + 1):
        left, right = pts[k - WINDOW : k], pts[k : k + WINDOW]
        if len({vers[j] for j in left + right}) > 1:
            continue
        a = [m for j in left if (m := meds[j]) is not None]
        b = [m for j in right if (m := meds[j]) is not None]
        ml = statistics.median(a)
        if not ml:
            continue
        delta = statistics.median(b) / ml - 1
        if abs(delta) >= least and (p := mannwhitney_p(a, b)) < ALPHA:
            # Rank by p, then by the gap between the windows' means: unlike the
            # medians, a mean moves when a window holds a point from the other side.
            score = (p, -abs(statistics.fmean(b) - statistics.fmean(a)) / ml, k)
            cands.append((pts[k], pts[k - 1], k, delta, p, score))
    # A step shows in every window straddling it; keep the split that separates best.
    keep = [c for c in cands if all(c[5] <= o[5] for o in cands if abs(o[2] - c[2]) < WINDOW)]
    return [[i, prev, round(d, 4), float(f"{p:.2g}")] for i, prev, _, d, p, _ in keep]


def _gate(
    steps: list[list[Any]], name: str, direction: str | None, gate: RegressionConfig
) -> list[list[Any]]:
    """Append to each step whether the gate allows it: worse, but within an allowlist rule."""
    for st in steps:
        verdict = None
        if direction is not None:
            verdict = gate.evaluate(
                name, st[2] * 100, higher_is_better=direction == "higher"
            ).verdict
        st.append(1 if verdict is Verdict.ALLOWED_OVER else 0)
    return steps


def suite_index(
    series: list[tuple[list[float | None], list[Any]]],
    n: int,
    direction: str | None,
    usable: list[bool],
) -> list[float | None]:
    """Per point, the geometric mean over benchmarks of median / that benchmark's typical median.

    The typical median is taken per version, so changed code doesn't move the index.
    Inverted for higher-is-better metrics, so up is always worse.
    """
    ratios: list[list[float]] = [[] for _ in range(n)]
    for meds, vers in series:
        by_ver: dict[Any, list[float]] = {}
        for m, v, ok in zip(meds, vers, usable, strict=True):
            if m and ok:
                by_ver.setdefault(v, []).append(m)
        ref = {v: statistics.median(ms) for v, ms in by_ver.items()}
        for i, (m, v) in enumerate(zip(meds, vers, strict=True)):
            if m and m > 0 and usable[i] and ref.get(v, 0) > 0:
                r = m / ref[v]
                ratios[i].append(1 / r if direction == "higher" else r)
    return [_sig(statistics.geometric_mean(r)) if r else None for r in ratios]


# ---- the page's data -----------------------------------------------------------------------


def build(
    path: Path,
    cfg: Config,
    *,
    repo: Repo | None = None,
    rev: str = "HEAD",
    where: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Reduce an archive to the page's data."""
    # load_runs types rows as BenchmarkResult; the helpers here read plain dicts.
    runs = cast(
        "list[tuple[list[dict[str, Any]], dict[str, Any]]]", load_runs(path, parse_where(where))
    )
    formats: dict[str, Any] = {}
    memory: set[str] = set()
    for rows, _ in runs:
        for r in rows:
            if r.get("skip"):
                continue
            for c in r.get("counters") or []:
                formats.setdefault(c["name"], c.get("format"))
            if isinstance(mem := r.get("memory"), dict):
                memory |= {f for f in MEMORY_FIELDS if isinstance(mem.get(f), int | float)}
    keys = [
        "real_time",
        "cpu_time",
        *(f"counters.{c}" for c in sorted(formats)),
        *(f"memory.{f}" for f in MEMORY_FIELDS if f in memory),
    ]
    metrics = [_metric_meta(k, formats, cfg.counters) for k in keys]
    gate = cfg.regressions
    threshold = gate.default_threshold / 100

    infos, chain, chain_start = _place(runs, repo, rev)
    by_machine: dict[str, list[int]] = {}
    for g, (_, prov) in enumerate(runs):
        by_machine.setdefault(machine_label(prov.get("context") or {}), []).append(g)

    versions: dict[str, int] = {}
    machines = []
    for label, members in sorted(by_machine.items(), key=lambda kv: -len(kv[1])):
        points = _points(members, infos)
        n = len(points)
        # Dirty or off the history (a branch): drawn, but not part of the history's steps.
        usable = [not p.dirty and not p.off for p in points]
        raw: dict[str, dict[str, list[list[float]]]] = {}
        vers: dict[str, list[int | None]] = {}
        for pos, p in enumerate(points):
            for g in p.runs:
                for r in runs[g][0]:
                    if r.get("skip"):
                        continue
                    name = r["name"]
                    if name not in vers:
                        vers[name] = [None] * n
                        raw[name] = {m["key"]: [[] for _ in range(n)] for m in metrics}
                    v = r.get("version")
                    vers[name][pos] = None if v is None else versions.setdefault(v, len(versions))
                    for key, x in _values(r):
                        if key in raw[name] and _finite(x):
                            raw[name][key][pos].append(float(x))
        bench: dict[str, dict[str, Any]] = {}
        index: dict[str, list[float | None]] = {}
        for m in metrics:
            for_index = []
            for name in sorted(raw):
                vals = raw[name][m["key"]]
                if not any(vals):
                    continue
                meds = [statistics.median(v) if v else None for v in vals]
                for_index.append((meds, vers[name]))
                sd = noise(meds, vers[name], usable)
                # A stricter allowlist rule finds smaller steps; a looser one marks them allowed.
                rule = gate.find_rule(name)
                least = min(threshold, rule.threshold / 100) if rule else threshold
                steps = find_steps(meds, vers[name], usable, least, sd)
                entry: dict[str, Any] = {
                    "m": [_sig(x) for x in meds],
                    "lo": _band(meds, [min(v) if v else None for v in vals]),
                    "hi": _band(meds, [max(v) if v else None for v in vals]),
                    "v": vers[name],
                    "steps": _gate(steps, name, m["dir"], gate),
                    "noise": None if sd is None else round(sd, 4),
                }
                if rule:
                    limit = rule.threshold if math.isfinite(rule.threshold) else None
                    entry["rule"] = [limit, rule.reason]
                bench.setdefault(name, {})[m["key"]] = entry
            index[m["key"]] = suite_index(for_index, n, m["dir"], usable)
        machines.append(
            {
                "label": label,
                "points": [
                    {
                        "c": p.commit,
                        "s": p.subject,
                        "d": infos[p.runs[-1]].date[:16].replace("T", " "),
                        "n": ", ".join(p.names),
                        "k": len(p.runs),
                        "dirty": p.dirty,
                        "ci": None if p.ci is None else p.ci - chain_start,
                        "off": p.off,
                        "rw": any(infos[g].rewritten for g in p.runs),
                        "ids": [infos[g].id for g in p.runs],
                    }
                    for p in points
                ],
                "bench": bench,
                "index": index,
            }
        )
    commit_url, compare_url = forge_links(cfg.project_root, repo)
    return {
        "title": path.name,
        "machines": machines,
        "chain": chain,
        "versions": list(versions),
        "metrics": metrics,
        "threshold": threshold,
        "window": WINDOW,
        "noiseK": NOISE_K,
        "rev": rev,
        "commitUrl": commit_url,
        "compareUrl": compare_url,
    }


def encode(data: Mapping[str, Any]) -> str:
    """The page's data as base64 of gzipped JSON. ``mtime=0`` keeps a page reproducible.

    Level 6: level 9 takes 12 times as long for 2% less.
    """
    raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
    return base64.b64encode(gzip.compress(raw, compresslevel=6, mtime=0)).decode()


def render(data: Mapping[str, Any]) -> str:
    """The page: the template with uPlot, the page's CSS and JS and the data inlined."""
    files = resources.files(__name__)
    parts = {
        "TITLE": html.escape(str(data["title"])),
        "UPLOT_CSS": (files / "uPlot.min.css").read_text(encoding="utf-8"),
        "CSS": (files / "report.css").read_text(encoding="utf-8"),
        "UPLOT_JS": (files / "uPlot.iife.min.js").read_text(encoding="utf-8"),
        "JS": (files / "report.js").read_text(encoding="utf-8"),
        "DATA": encode(data),
    }
    # One pass, so a placeholder-like string inside an inlined file stays as it is.
    page = (files / "page.html").read_text(encoding="utf-8")
    return re.sub(r"__([A-Z_]+)__", lambda m: parts.get(m[1], m[0]), page)


@dataclass(frozen=True, slots=True)
class Summary:
    """What :func:`write` wrote, for the command's closing line."""

    machines: int
    runs: int
    steps: int  # in real time
    size: int  # bytes


def write(
    archive: Path,
    output: Path,
    cfg: Config,
    *,
    revision: str | None = None,
    where: Mapping[str, str] | None = None,
) -> Summary:
    """Write ``archive``'s report to ``output``.

    The repository is the one ``cfg.project_root`` is in; outside one, or before
    its first commit, points follow run dates. ``revision`` defaults to ``HEAD``;
    one given explicitly raises ``ValueError`` outside a repository or for a
    commit the repository lacks.
    """
    repo = Repo.find(cfg.project_root)
    if revision is not None and repo is None:
        raise ValueError("--revision: not in a git repository")
    rev = revision or "HEAD"
    if repo is not None and not repo.has_revision(rev):
        if revision is not None:
            raise ValueError(f"--revision: no commit {revision!r} in {repo.path}")
        repo = None  # a repository without commits yet
    data = build(archive, cfg, repo=repo, rev=rev, where=where)
    page = render(data)
    try:
        output.write_text(page, encoding="utf-8")
    except OSError as e:
        reason = e.strerror.lower() if e.strerror else f"cannot write ({e})"
        raise type(e)(f"{reason}: {output}") from e
    steps = sum(
        len(b["real_time"]["steps"])
        for m in data["machines"]
        for b in m["bench"].values()
        if "real_time" in b
    )
    runs = sum(p["k"] for m in data["machines"] for p in m["points"])
    return Summary(len(data["machines"]), runs, steps, len(page.encode()))
