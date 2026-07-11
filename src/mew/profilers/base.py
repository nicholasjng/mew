"""Shared types for out-of-process, native-frame profilers.

These backends (xctrace, py-spy, perf) launch :mod:`mew._subprocess_worker` to
drive one benchmark case while sampling it from the outside. They produce an
artifact (trace / flamegraph / profile file) rather than the scalar summaries the
in-process samplers (pyinstrument via ``mew run --sample``, memray) attach to
timed ``Run`` rows.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mew._profile import iter_entry_cases

if TYPE_CHECKING:
    from mew._registry import Entry


@runtime_checkable
class Profiler(Protocol):
    """An out-of-process profiler backend."""

    name: str
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


def each_case(
    entries: list[Entry],
    *,
    output_dir: Path,
    ext: str,
) -> Iterator[tuple[str, str, str, int, Path]]:
    """Yield ``(key, file, entry_name, case, dest)`` per case for one-artifact-per-case backends.

    Creates ``output_dir`` and skips entries with no source file (nothing to launch).
    ``dest`` is ``output_dir/<slug(key)><ext>``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry.file is None:
            print(f"mew: skipping {entry.name}: no source file to launch", file=sys.stderr)
            continue
        for key, rng in iter_entry_cases(entry):
            yield key, entry.file, entry.name, rng, output_dir / f"{slug(key)}{ext}"


def parse_seconds(dur: str, *, flag: str = "--time-limit") -> float:
    """``'10s'`` / ``'500ms'`` / ``'1m'`` / ``'5'`` → float seconds.

    For backends without a native duration flag (perf wraps the worker in ``timeout``;
    py-spy takes integer ``--duration`` seconds) and for duration-valued run flags
    (``--min-warmup-time``). xctrace passes its ``--time-limit`` string through
    unparsed. An unparseable value is a CLI error naming ``flag``, not a traceback.
    """
    dur = dur.strip()
    try:
        if dur.endswith("ms"):  # before "m": "500ms" is not minutes
            return float(dur[:-2]) / 1000
        if dur.endswith("m"):
            return float(dur[:-1]) * 60
        return float(dur.removesuffix("s"))
    except ValueError:
        raise SystemExit(
            f"mew: invalid {flag} {dur!r}; use seconds ('10s', '0.5'), "
            "milliseconds ('500ms'), or minutes ('1m')"
        ) from None
