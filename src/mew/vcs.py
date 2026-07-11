"""Source-control provenance as a context provider.

:func:`vcs_context` returns a mapping to hand to :func:`mew.update_context`, so a
suite records what it was built from::

    import mew

    mew.update_context(mew.vcs_context())

Opt-in: it shells out to jj or git, which not every run wants to pay for, and a
suite outside a work tree has nothing to record. The values land under
``custom.vcs`` in the context and on every stored row, where ``mew compare``
groups runs by ``custom.vcs.commit`` when no explicit ``--session-tag`` is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["vcs_context"]


def _run(cwd: Path | None, program: str, *args: str) -> str | None:
    """Stripped stdout of ``program args``, or None on failure/empty."""
    # Deferred: keeps subprocess (~6ms) off the `import mew` path.
    import shutil
    import subprocess

    if shutil.which(program) is None:
        return None
    try:
        proc = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=5, cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _jj(cwd: Path | None) -> dict[str, Any] | None:
    # One template, one process: change id, commit id, and whether the working
    # copy differs from its parent. --ignore-working-copy reads without snapshotting.
    out = _run(
        cwd,
        "jj",
        "log",
        "--no-graph",
        "--ignore-working-copy",
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
    # Tracked changes only: an untracked results file or build artifact sitting in
    # the tree does not change what was benchmarked. `_run` maps empty output to None.
    info["dirty"] = _run(cwd, "git", "status", "--porcelain", "--untracked-files=no") is not None
    branch = _run(cwd, "git", "rev-parse", "--abbrev-ref", "HEAD")
    # Detached HEAD reports the literal "HEAD", which names nothing.
    if branch and branch != "HEAD":
        info["branch"] = branch
    return info


def vcs_context(cwd: Path | None = None) -> dict[str, Any]:
    """Source-control provenance for ``cwd``, as ``{"vcs": {...}}``.

    Tries jj, then git; returns ``{}`` outside a work tree or when neither tool
    is installed, so ``update_context(vcs_context())`` is always safe to call.

    The block carries ``backend`` and the full ``commit`` (plus ``dirty``, and
    ``change_id`` under jj / ``branch`` under git). ``commit`` is full-length on
    purpose: it is what ``mew compare`` groups runs by, and an abbreviation can
    collide as history grows.
    """
    info = _jj(cwd) or _git(cwd)
    return {"vcs": info} if info else {}
