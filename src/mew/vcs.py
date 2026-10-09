"""Source-control provenance as a context provider.

:func:`vcs_context` returns a mapping to hand to :func:`mew.update_context`, so a
suite records what it was built from::

    import mew

    mew.update_context(mew.vcs_context())

The provider shells out to jj or git and returns nothing outside a work tree.
Results store it under ``context.vcs``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["vcs_context"]


def _run(cwd: Path | None, program: str, *args: str) -> str | None:
    """Stripped stdout of ``program args``, or None on failure/empty."""
    import subprocess

    # A missing program raises FileNotFoundError, an OSError.
    try:
        proc = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=5, cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _jj(cwd: Path | None) -> dict[str, Any] | None:
    # One process for all fields. Lets jj snapshot the working copy first, so
    # uncommitted edits show up as dirty.
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
    if out is None:
        return None
    parts = out.splitlines()
    if len(parts) != 3:
        return None
    change_id, commit, state = parts
    return {"backend": "jj", "change_id": change_id, "commit": commit, "dirty": state == "dirty"}


def _git(cwd: Path | None) -> dict[str, Any] | None:
    commit = _run(cwd, "git", "rev-parse", "HEAD")
    if commit is None:
        return None
    info: dict[str, Any] = {"backend": "git", "commit": commit}
    # Tracked changes only: untracked results or build artifacts don't change what
    # was benchmarked. Empty output (clean) comes back as None.
    info["dirty"] = _run(cwd, "git", "status", "--porcelain", "--untracked-files=no") is not None
    branch = _run(cwd, "git", "rev-parse", "--abbrev-ref", "HEAD")
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
    """
    info = _jj(cwd) or _git(cwd)
    return {"vcs": info} if info else {}
