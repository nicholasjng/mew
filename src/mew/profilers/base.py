"""Shared types for out-of-process, native-frame profilers.

These backends (xctrace, py-spy, perf) all launch :mod:`mew._subprocess_worker`
to drive one benchmark case while sampling it from the outside. They produce an
artifact (a trace / flamegraph / profile file) rather than the scalar summaries
that the in-process samplers (pyinstrument via ``mew run --sample``, memray)
attach to timed ``Run`` rows.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mew._registry import Entry


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a profiler can do, used for ``auto`` selection and messaging."""

    #: Captures native (C/C++) stack frames — the reason these backends exist.
    native_frames: bool
    #: ``sys.platform`` values the backend supports.
    platforms: frozenset[str]


@runtime_checkable
class Profiler(Protocol):
    """An out-of-process profiler backend."""

    name: str
    capabilities: Capabilities
    #: Human-facing hint for where to view the artifact, e.g. ``"Instruments.app"``.
    viewer_hint: str

    def unavailable_reason(self) -> str | None:
        """Return ``None`` if usable here, else a short reason (missing tool, wrong OS)."""
        ...

    def run(
        self,
        entries: list[Entry],
        *,
        output_dir: Path,
        iterations: int,
        time_limit: str | None = None,
        **opts: object,
    ) -> dict[str, Path]:
        """Record each case; return artifact paths keyed like :func:`iter_entry_cases`."""
        ...

    def open_artifact(self, path: Path) -> None:
        """Open ``path`` in the backend's viewer (best-effort; may be a no-op)."""
        ...


def worker_argv(*, file: str, entry_name: str, case: int, iterations: int) -> list[str]:
    """The shared ``python -m mew._subprocess_worker ...`` tail every backend wraps."""
    return [
        sys.executable,
        "-m",
        "mew._subprocess_worker",
        "--file",
        file,
        "--entry",
        entry_name,
        "--case",
        str(case),
        "--iterations",
        str(iterations),
    ]


def slug(key: str) -> str:
    """Filesystem-safe stem for a profile key like ``bench.py::f/case:0``."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-") or "bench"
