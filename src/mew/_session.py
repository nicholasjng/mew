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


def derive_session_tag(cwd: Path | None = None) -> str | None:
    """``git describe --always --dirty``, or None outside a work tree.

    The label an archive almost always wants is "what code was this", so derive it instead
    of making every pipeline spell it out. Works in jj-colocated checkouts (a ``.git`` is present).
    """
    try:
        proc = subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = proc.stdout.strip()
    return tag if proc.returncode == 0 and tag else None
