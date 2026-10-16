"""Machine context: what the benchmarks ran on.

Unlike :func:`mew.vcs_context`, :func:`mew.run` applies this provider by default,
so its fields are part of every run's context.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from typing import Any

from mew import _core

__all__ = ["gil_enabled", "machine_context"]


def gil_enabled() -> bool:
    """Return whether the GIL is enabled.

    ``True`` on a stock interpreter. ``False`` on a free-threaded build, unless
    something enabled the GIL at run time, such as importing an extension module
    that doesn't declare free-threading support.

    Returns
    -------
    bool
        Whether the GIL is enabled right now.
    """
    return getattr(sys, "_is_gil_enabled", lambda: True)()


@contextmanager
def _silence_native_stderr() -> Generator[None]:
    """Redirect OS-level fd 2 to /dev/null, for GB probes that bypass ``sys.stderr``.

    Never wrap the run itself: user output and GB run-time diagnostics must stay visible.
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


def _sysctl_string(name: bytes) -> str | None:
    import ctypes

    libc = ctypes.CDLL(None)
    size = ctypes.c_size_t()
    if libc.sysctlbyname(name, None, ctypes.byref(size), None, 0):
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(name, buf, ctypes.byref(size), None, 0):
        return None
    return buf.value.decode() or None


def _cpuinfo_model(lines: Iterable[str]) -> str | None:
    """``model name`` from /proc/cpuinfo, which Arm64 kernels leave out."""
    for line in lines:
        key, _, value = line.partition(":")
        if key.strip() == "model name":
            return value.strip() or None
    return None


def _cpu_model() -> str | None:
    """The name the OS gives the CPU, or ``None`` where it gives none."""
    try:
        if sys.platform == "darwin":
            return _sysctl_string(b"machdep.cpu.brand_string")
        if sys.platform == "win32":
            import winreg

            path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip() or None
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            return _cpuinfo_model(f)
    except OSError:
        return None


def machine_context() -> dict[str, Any]:
    """Return what the benchmarks run on; :func:`mew.run` records it in every run.

    Returns
    -------
    dict[str, Any]
        ``cpu_model``, the name the OS gives the CPU, such as ``"Apple M2"``,
        or ``None`` where it gives none, as on Linux on Arm; ``num_cpus``;
        ``cpu_scaling_enabled``, ``True`` only when frequency scaling was
        detected; and ``python``, the interpreter's ``version``,
        ``implementation`` and ``gil_enabled``.
    """
    with _silence_native_stderr():
        info = _core.cpu_info()
    return {
        "cpu_model": _cpu_model(),
        "num_cpus": info["num_cpus"],
        "cpu_scaling_enabled": info["cpu_scaling_enabled"],
        "python": {
            "version": platform.python_version(),
            "implementation": sys.implementation.name,
            "gil_enabled": gil_enabled(),
        },
    }
