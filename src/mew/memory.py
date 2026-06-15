"""Optional memory profiling via memray."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from mew._profile import _ProfileState, iter_entry_cases

if TYPE_CHECKING:
    from mew._registry import Entry
    from mew._typing import BenchmarkFn


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    """Per-case memory summary captured by memray.

    The capture is scoped to the timing loop (``for _ in state``), so
    fixture/setup allocations are excluded; ``iterations`` measured passes run
    after a warmup, making ``allocations_per_iteration`` a steady-state figure.

    Attributes
    ----------
    peak_bytes : int
        Peak memory during the loop (``metadata.peak_memory``); a high-water
        mark, independent of iteration count.
    total_bytes : int
        Tracked heap live at the high-water mark, *not* the cumulative sum.
    total_allocations : int
        Cumulative allocation count across all ``iterations``. Not comparable
        across runs of differing iteration count — use ``allocations_per_iteration``.
    iterations : int
        Number of measured timing-loop iterations the capture ran over.
    allocations_per_iteration : float
        ``total_allocations / iterations`` — the per-call count, comparable
        across engines regardless of speed.
    """

    profiler: str
    peak_bytes: int
    total_bytes: int
    total_allocations: int
    iterations: int
    allocations_per_iteration: float


def profile(
    entries: list[Entry],
    *,
    flamegraph: Path | None = None,
    iterations: int = 100,
) -> dict[str, MemoryProfile]:
    """Profile each entry with memray over ``iterations`` measured loop passes.

    Parameters
    ----------
    entries : list[Entry]
        Benchmarks to profile.
    flamegraph : Path, optional
        If given, additionally writes a combined HTML flame graph to this path.
    iterations : int, default 100
        Measured timing-loop passes per case (a warmup runs first, untracked).
        Many passes amortize one-time allocations, keeping
        ``allocations_per_iteration`` comparable across engines.

    Returns
    -------
    dict[str, MemoryProfile]
        Per-case profiles keyed by ``entry.name`` (or ``entry.name/case:<i>`` for
        each variant of a parametrized family).
    """
    if find_spec("memray") is None:
        raise SystemExit(
            "memray is required for memory profiling. "
            "Install it with: uv add --optional memory memray"
        )
    profiles = _collect_stats(entries, max(1, iterations))
    if flamegraph is not None:
        _write_flamegraph(entries, flamegraph)
    return profiles


def _capture_case(fn: BenchmarkFn, rng: int, dest: Path, iterations: int, warmup: int) -> bool:
    """Capture one case with memray over ``iterations`` loop passes (after ``warmup``).

    The warmup runs untracked so one-time allocations don't dominate; the tracker
    spans only the measured loop. Returns False when the body never iterated its state.
    """
    import memray

    # Warmup outside the tracker to trigger one-time allocations.
    if warmup > 0:
        fn(_ProfileState(n_iterations=warmup, range_value=rng))

    tracker = memray.Tracker(dest)
    entered = exited = False

    def start() -> None:
        nonlocal entered
        entered = True
        tracker.__enter__()

    def stop() -> None:
        nonlocal exited
        exited = True
        tracker.__exit__(None, None, None)

    try:
        fn(
            _ProfileState(
                n_iterations=iterations, range_value=rng, on_loop_start=start, on_loop_end=stop
            )
        )
    finally:
        # A body that raises mid-loop leaves the tracker open; close it so the
        # capture file is readable and the next case can start fresh.
        if entered and not exited:
            tracker.__exit__(None, None, None)
    return entered


def _collect_stats(entries: list[Entry], iterations: int) -> dict[str, MemoryProfile]:
    import memray

    # A tenth of the measured count (>=1) is enough to clear lazy init.
    warmup = max(1, iterations // 10)
    profiles: dict[str, MemoryProfile] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        i = 0
        for entry in entries:
            for key, rng in iter_entry_cases(entry):
                dest = root / f"capture-{i}.bin"
                i += 1
                if not _capture_case(entry.fn, rng, dest, iterations, warmup):
                    print(
                        f"warning: {key}: body never iterated its state; skipping memory capture",
                        file=sys.stderr,
                    )
                    continue
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
                    iterations=iterations,
                    allocations_per_iteration=total_allocs / iterations,
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
