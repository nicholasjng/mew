"""Source-control provenance from jj or git.

:func:`vcs_context` returns the working copy's commit and dirty state as a
mapping to merge into a run's context, as in a setup file's ``context()`` hook::

    import mew

    def context():
        return {**mew.vcs_context(), "dataset": "uniform-1k"}

It shells out to jj, then git, and returns ``{}`` outside a work tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["vcs_context"]


def _run(cwd: Path | None, program: str, *args: str) -> str | None:
    """Stripped stdout of ``program args``, ``""`` when it prints nothing, or None on failure."""
    import subprocess

    # A missing program raises FileNotFoundError, an OSError.
    try:
        proc = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=5, cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _jj(cwd: Path | None) -> dict[str, Any] | None:
    # One process for all fields. jj snapshots the working copy first,
    # so uncommitted edits show up as dirty.
    out = _run(
        cwd,
        "jj",
        "log",
        "--no-graph",
        "-r",
        "@",
        "-T",
        'change_id.short(12) ++ "\\n" ++ commit_id ++ "\\n" ++ if(empty, "clean", "dirty")',
    )
    if not out:
        return None
    parts = out.splitlines()
    if len(parts) != 3:
        return None
    change_id, commit, state = parts
    return {"backend": "jj", "change_id": change_id, "commit": commit, "dirty": state == "dirty"}


def _git(cwd: Path | None) -> dict[str, Any] | None:
    out = _run(cwd, "git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD")
    if not out:
        return None
    commit, _, branch = out.partition("\n")
    info: dict[str, Any] = {"backend": "git", "commit": commit}
    # Tracked changes only: untracked results or build artifacts don't change what
    # was benchmarked. A failed status, e.g. a timeout, leaves the state unknown
    # rather than clean.
    status = _run(cwd, "git", "status", "--porcelain", "--untracked-files=no")
    if status is not None:
        info["dirty"] = bool(status)
    # Detached HEAD reports the literal "HEAD", which names nothing.
    if branch and branch != "HEAD":
        info["branch"] = branch
    return info


def vcs_context(cwd: Path | None = None) -> dict[str, Any]:
    """Return source-control provenance for a working directory.

    Tries jj, then git; returns ``{}`` outside a work tree or when neither tool
    is installed.

    Parameters
    ----------
    cwd : Path, optional
        Directory to inspect. Defaults to the current directory.

    Returns
    -------
    dict[str, Any]
        ``{"vcs": {...}}`` with backend, commit, and dirty state, or ``{}``.
        ``dirty`` is left out when ``git status`` fails, e.g. times out.
    """
    info = _jj(cwd) or _git(cwd)
    return {"vcs": info} if info else {}
