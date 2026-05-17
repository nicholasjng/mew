"""Optional memory profiling via memray."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mew._profile import _MockState

if TYPE_CHECKING:
    from mew._registry import Entry


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    profiler: str
    peak_bytes: int
    total_bytes: int
    total_allocations: int


def _require_memray() -> Any:
    try:
        import memray  # type: ignore[import-not-found]

        return memray
    except ImportError:
        raise SystemExit(
            "memray is required for memory profiling. "
            "Install it with: uv add --optional memory memray"
        ) from None


def profile(
    entries: list[Entry],
    *,
    flamegraph: Path | None = None,
) -> dict[str, MemoryProfile]:
    """Profile each entry once with memray and return per-entry MemoryProfiles.

    If *flamegraph* is given, also runs a combined pass over all entries and
    writes an HTML flame graph to that path.
    """
    memray = _require_memray()
    profiles = _collect_stats(entries, memray)
    if flamegraph is not None:
        _write_flamegraph(entries, flamegraph, memray)
    return profiles


def _collect_stats(entries: list[Entry], memray: Any) -> dict[str, MemoryProfile]:
    profiles: dict[str, MemoryProfile] = {}
    for entry in entries:
        dest = Path(tempfile.mktemp(suffix=".bin"))
        try:
            with memray.Tracker(str(dest)):
                entry.fn(_MockState())
            reader = memray.FileReader(str(dest))
            records = list(reader.get_allocation_records())
            total_bytes = sum(r.size for r in records if r.size > 0)
            total_allocs = sum(r.n_allocations for r in records if r.size > 0)
            try:
                snapshots = list(reader.get_memory_snapshots())
                peak = max((s.rss for s in snapshots), default=0) or total_bytes
            except Exception:
                peak = total_bytes
            profiles[entry.name] = MemoryProfile(
                profiler="memray",
                peak_bytes=peak,
                total_bytes=total_bytes,
                total_allocations=total_allocs,
            )
        finally:
            dest.unlink(missing_ok=True)
    return profiles


def _write_flamegraph(entries: list[Entry], path: Path, memray: Any) -> None:
    combined = Path(tempfile.mktemp(suffix=".bin"))
    try:
        with memray.Tracker(str(combined)):
            for entry in entries:
                entry.fn(_MockState())
        subprocess.run(
            [sys.executable, "-m", "memray", "flamegraph", "-o", str(path), str(combined)],
            check=True,
        )
    finally:
        combined.unlink(missing_ok=True)
