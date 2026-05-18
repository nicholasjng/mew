"""Optional memory profiling via memray."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from mew._profile import _MockState

if TYPE_CHECKING:
    from mew._registry import Entry


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    profiler: str
    peak_bytes: int
    total_bytes: int
    total_allocations: int


def _ensure_memray() -> None:
    if find_spec("memray") is None:
        raise SystemExit(
            "memray is required for memory profiling. "
            "Install it with: uv add --optional memory memray"
        )


def profile(
    entries: list[Entry],
    *,
    flamegraph: Path | None = None,
) -> dict[str, MemoryProfile]:
    """Profile each entry once with memray and return per-entry MemoryProfiles.

    If *flamegraph* is given, also runs a combined pass over all entries and
    writes an HTML flame graph to that path.
    """
    _ensure_memray()
    profiles = _collect_stats(entries)
    if flamegraph is not None:
        _write_flamegraph(entries, flamegraph)
    return profiles


def _collect_stats(entries: list[Entry]) -> dict[str, MemoryProfile]:
    import memray

    profiles: dict[str, MemoryProfile] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for i, entry in enumerate(entries):
            dest = root / f"capture-{i}.bin"
            with memray.Tracker(dest):
                entry.fn(_MockState())
            reader = memray.FileReader(dest)
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
    return profiles


def _write_flamegraph(entries: list[Entry], path: Path) -> None:
    import memray
    from memray.reporters.flamegraph import FlameGraphReporter

    with tempfile.TemporaryDirectory() as tmpdir:
        combined = Path(tmpdir) / "combined.bin"
        with memray.Tracker(combined):
            for entry in entries:
                entry.fn(_MockState())
        reader = memray.FileReader(combined)
        reporter = FlameGraphReporter.from_snapshot(
            reader.get_high_watermark_allocation_records(merge_threads=True),
            memory_records=tuple(reader.get_memory_snapshots()),
            native_traces=False,
        )
        with path.open("w") as f:
            reporter.render(
                f,
                metadata=reader.metadata,
                show_memory_leaks=False,
                merge_threads=True,
                inverted=False,
            )
