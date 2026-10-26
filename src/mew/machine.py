"""Machine provenance: the CPU and interpreter a process runs on."""

from __future__ import annotations

import os
import platform
import sys
import sysconfig
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from typing import Any

from mew import _core

__all__ = ["gil_enabled", "is_free_threaded_build", "machine_context"]


def is_free_threaded_build() -> bool:
    """Return whether this interpreter was built with free-threading support.

    This stays true when a free-threaded build runs with the GIL enabled.
    Use :func:`gil_enabled` to check the current runtime state.

    Returns
    -------
    bool
        Whether ``Py_GIL_DISABLED`` is set in the interpreter's build configuration.
    """
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def gil_enabled() -> bool:
    """Return whether the GIL is enabled.

    ``True`` on a stock interpreter. ``False`` on a free-threaded build, unless
    something enabled the GIL at run time, such as importing an extension module
    that doesn't declare free-threading support.
    Use :func:`is_free_threaded_build` to check the interpreter's build configuration.

    Returns
    -------
    bool
        Whether the GIL is enabled right now.
    """
    return getattr(sys, "_is_gil_enabled", lambda: True)()


@contextmanager
def _silence_native_stderr() -> Generator[None]:
    """Redirect OS-level fd 2 to /dev/null within the context.

    It silences native writes that bypass ``sys.stderr``, such as Google
    Benchmark's CPU probes. Never wrap the run itself: user output and GB
    run-time diagnostics must stay visible.
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
    """Describe the machine and interpreter this process runs on.

    Returns
    -------
    dict[str, Any]
        ``cpu_model``, the name the OS gives the CPU, such as ``"Apple M2"``,
        or ``None`` where it gives none, as on Linux on Arm; ``num_cpus``;
        ``cpu_scaling_enabled``, ``True`` only when frequency scaling was
        detected; and ``python``, the interpreter's ``version``,
        ``implementation``, ``free_threaded_build``, and ``gil_enabled``.
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
            "free_threaded_build": is_free_threaded_build(),
            "gil_enabled": gil_enabled(),
        },
    }
