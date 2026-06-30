"""Session identity for benchmark runs.

Each :func:`mew.run` invocation is one *session*: a time-ordered ``session_id``
(UUIDv7) plus an optional human ``session_tag``. Reporters persist both, so a
result file holding several runs stays addressable (`mew compare path@tag`)
instead of collapsing to "latest by timestamp" with second-granularity ties.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path


def new_session_id() -> str:
    """A UUIDv7 (RFC 9562) string: 48-bit unix-ms timestamp, then random bits.

    Time-ordered by construction, so the lexicographically greatest id in a file is the
    latest session. Stdlib ``uuid.uuid7`` arrives in 3.14; hand-rolled here for 3.12/3.13.
    """
    unix_ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2)) & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8)) & 0x3FFF_FFFF_FFFF_FFFF
    value = (
        ((unix_ms & 0xFFFF_FFFF_FFFF) << 80)  # unix_ts_ms
        | (0x7 << 76)  # version 7
        | (rand_a << 64)
        | (0b10 << 62)  # RFC 4122/9562 variant
        | rand_b
    )
    return str(uuid.UUID(int=value))


# Built-in describe commands (args after the program name). jj uses a fixed-length
# change-id prefix (not ``shortest()``, which grows with the repo) and no
# ``-dirty`` marker, since jj's working copy is always a commit. With no tool
# configured, derivation tries jj then git.
_PRESETS: dict[str, list[str]] = {
    "git": ["describe", "--always", "--dirty"],
    # --ignore-working-copy: read the repo without snapshotting/mutating it.
    "jj": ["log", "--no-graph", "--ignore-working-copy", "-r", "@", "-T", "change_id.short(12)"],
}


def derive_session_tag(
    cwd: Path | None = None, *, tool: str | None = None, args: list[str] | None = None
) -> str | None:
    """The run's source-revision label, or None outside a work tree / on failure.

    ``tool`` is the executable that prints the tag and ``args`` its arguments. With no
    ``tool``, derive automatically: try jj, then git. A known ``tool`` (``"git"`` /
    ``"jj"``) supplies default ``args``; any other command brings its own (none by
    default), so a project can plug in ``hg``, a script, etc. without being tied to a VCS.
    """
    if tool is None:
        return _jj_describe(cwd) or _git_describe(cwd)
    return _run_vcs(cwd, tool, args if args is not None else _PRESETS.get(tool, []))


def _run_vcs(cwd: Path | None, program: str, args: list[str]) -> str | None:
    """Run ``program args`` and return its stripped stdout, or None on failure/empty."""
    try:
        proc = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=5, cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _git_describe(cwd: Path | None) -> str | None:
    return _run_vcs(cwd, "git", _PRESETS["git"])


def _jj_describe(cwd: Path | None) -> str | None:
    return _run_vcs(cwd, "jj", _PRESETS["jj"])
