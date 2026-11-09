"""Provenance for a run's context: the machine, the interpreter and source control.

:func:`machine_context` describes the CPU and interpreter, and every run
records it. :func:`vcs_context` describes the working copy's commit, from jj or
git; a setup file's ``context()`` hook adds it where wanted::

    import mew

    def context():
        return mew.vcs_context()
"""

from __future__ import annotations

import os
import platform
import sys
import sysconfig
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mew import _core

__all__ = ["machine_context", "vcs_context"]


def _is_free_threaded_build() -> bool:
    """Return whether this interpreter was built with free-threading support.

    This stays true when a free-threaded build runs with the GIL enabled.
    Use :func:`_gil_enabled` to check the current runtime state.

    Returns
    -------
    bool
        Whether ``Py_GIL_DISABLED`` is set in the interpreter's build configuration.
    """
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def _gil_enabled() -> bool:
    """Return whether the GIL is enabled.

    ``True`` on a stock interpreter. ``False`` on a free-threaded build, unless
    something enabled the GIL at run time, such as importing an extension module
    that doesn't declare free-threading support.
    Use :func:`_is_free_threaded_build` to check the interpreter's build configuration.

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
            "free_threaded_build": _is_free_threaded_build(),
            "gil_enabled": _gil_enabled(),
        },
    }


def _run(cwd: Path | None, program: str, *args: str) -> str | None:
    """Stripped stdout of ``program args``, ``""`` when it prints nothing, or None on failure."""
    import subprocess

    # A missing program raises FileNotFoundError, an OSError.
    try:
        proc = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=5, cwd=cwd, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _jj(cwd: Path | None) -> dict[str, Any] | None:
    # One process for all fields. jj snapshots the working copy first,
    # so uncommitted edits show up as dirty.
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
    if not out:
        return None
    parts = out.splitlines()
    if len(parts) != 3:
        return None
    change_id, commit, state = parts
    return {"backend": "jj", "change_id": change_id, "commit": commit, "dirty": state == "dirty"}


def _git(cwd: Path | None) -> dict[str, Any] | None:
    out = _run(cwd, "git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD")
    if not out:
        return None
    commit, _, branch = out.partition("\n")
    info: dict[str, Any] = {"backend": "git", "commit": commit}
    # Tracked changes only: untracked results or build artifacts don't change what
    # was benchmarked. A failed status, e.g. a timeout, leaves the state unknown
    # rather than clean.
    status = _run(cwd, "git", "status", "--porcelain", "--untracked-files=no")
    if status is not None:
        info["dirty"] = bool(status)
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
        ``dirty`` is left out when ``git status`` fails, e.g. times out.
    """
    info = _jj(cwd) or _git(cwd)
    return {"vcs": info} if info else {}
