"""Session identifiers for benchmark runs."""

from __future__ import annotations

import os
import time
import uuid


def new_session_id() -> str:
    """Return a time-ordered UUIDv7 string."""
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
