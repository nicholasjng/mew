"""Out-of-process, native-frame profiler backends for ``mew profile``.

These capture C/native stacks by sampling the whole process from outside, what
in-process samplers can't do. In-process Python sampling (pyinstrument) lives on
``mew run --sample`` and is deliberately *not* a backend here.

``select()`` resolves a ``--profiler`` value, including ``auto`` (picks the
platform's native backend or errors with a pointer to ``mew run --sample``).
"""

from __future__ import annotations

import sys

from mew.profilers.base import Capabilities, Profiler, worker_argv
from mew.profilers.perf import PerfProfiler
from mew.profilers.pyspy import PySpyProfiler
from mew.profilers.xctrace import XctraceProfiler

__all__ = [
    "Capabilities",
    "Profiler",
    "select",
    "worker_argv",
]

#: All backends, keyed by their ``--profiler`` name.
_BACKENDS: dict[str, Profiler] = {
    p.name: p for p in (XctraceProfiler(), PySpyProfiler(), PerfProfiler())
}

#: ``auto`` preference order per platform. py-spy is listed before perf on Linux:
#: it merges Python + native stacks and usually needs no privilege tuning.
_AUTO_ORDER: dict[str, list[str]] = {
    "darwin": ["xctrace"],
    "linux": ["py-spy", "perf"],
    "win32": ["py-spy"],
}

_NO_NATIVE_HINT = (
    "Install one (macOS: Xcode for xctrace; Linux: py-spy or perf), or use "
    "`mew run --sample` for in-process Python-level sampling (no native frames)."
)


def select(name: str) -> Profiler:
    """Resolve a ``--profiler`` value to an available backend, or exit with guidance."""
    if name != "auto":
        backend = _BACKENDS.get(name)
        if backend is None:
            choices = ", ".join(["auto", *sorted(_BACKENDS)])
            raise SystemExit(f"mew: unknown profiler {name!r} (choose from: {choices})")
        if reason := backend.unavailable_reason():
            raise SystemExit(f"mew: profiler {name!r} is unavailable: {reason}")
        return backend

    for cand in _AUTO_ORDER.get(sys.platform, []):
        if _BACKENDS[cand].unavailable_reason() is None:
            return _BACKENDS[cand]
    raise SystemExit(f"mew: no native-frame profiler is available here. {_NO_NATIVE_HINT}")
