"""Machine context: what the benchmarks ran on.

:func:`machine_context` is a context provider like :func:`mew.vcs_context`, but
:func:`mew.run` applies it by default: ``cpu_scaling_enabled`` is the signal that
says whether the numbers can be trusted at all, and a tool that silently stops
reporting it is worse than one that always does.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mew import _core

__all__ = ["machine_context"]


@contextmanager
def _silence_native_stderr() -> Iterator[None]:
    """Redirect OS-level fd 2 to /dev/null within the scope.

    Google Benchmark's lazy system-info probes write platform diagnostics straight
    to fd 2, bypassing ``sys.stderr``. Scope this narrowly, never around the
    benchmark run itself: user output and GB's run-time diagnostics must stay
    visible.
    """
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(2)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(saved)
        os.close(devnull)


def machine_context() -> dict[str, Any]:
    """Return CPU count and frequency-scaling state.

    ``cpu_scaling_enabled`` is ``True`` only when scaling was positively
    detected.

    Returns
    -------
    dict[str, Any]
        Machine metadata suitable for benchmark context.
    """
    with _silence_native_stderr():
        info = _core.cpu_info()
    return {
        "num_cpus": info["num_cpus"],
        "cpu_scaling_enabled": info["cpu_scaling"] == "enabled",
    }
