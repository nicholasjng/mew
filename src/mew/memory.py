"""Optional memory profiling via memray."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from mew._profile import _ProfileState, iter_entry_cases

if TYPE_CHECKING:
    from mew._registry import Entry


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    """Per-(case) memory summary captured by memray.

    Attributes
    ----------
    peak_bytes : int
        Peak resident set size (``metadata.peak_memory``).
    total_bytes : int
        Tracked heap live at the high-water mark, *not* the cumulative sum.
    total_allocations : int
        Cumulative allocation count (``metadata.total_allocations``).
    """

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
    """Profile each entry once with memray.

    Parameters
    ----------
    entries : list[Entry]
        Benchmarks to profile.
    flamegraph : Path, optional
        If given, additionally writes a combined HTML flame graph to this path.

    Returns
    -------
    dict[str, MemoryProfile]
        Per-case profiles keyed by ``entry.name`` (or ``entry.name/case:<i>`` for
        each variant of a parametrized family).
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
        i = 0
        for entry in entries:
            for key, rng in iter_entry_cases(entry):
                dest = root / f"capture-{i}.bin"
                i += 1
                with memray.Tracker(dest):
                    entry.fn(_ProfileState(range_value=rng))
                reader = memray.FileReader(dest)
                meta = reader.metadata
                # From metadata, not get_allocation_records(): that scan is O(every
                # allocation) — minutes and gigabytes on an allocation-heavy body.
                peak = meta.peak_memory
                total_allocs = meta.total_allocations
                # Live bytes at the high-water mark: bounded by peak concurrent allocs.
                hwm = reader.get_high_watermark_allocation_records(merge_threads=True)
                total_bytes = sum(r.size for r in hwm)
                profiles[key] = MemoryProfile(
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
                for _, rng in iter_entry_cases(entry):
                    entry.fn(_ProfileState(range_value=rng))
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
