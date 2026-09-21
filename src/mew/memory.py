"""Memory profiling with memray and Google Benchmark's memory manager."""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from mew._typing import MemoryMetrics
from mew.machine import _gil_enabled

if TYPE_CHECKING:
    from memray import Tracker

# A memray stack frame: ``(function, filename, lineno)``.
Frame = tuple[str, str, int]

# Free-threaded CPython serves object allocations from mimalloc, which never
# reaches the system allocator memray hooks by default, so track them separately.
_TRACE_PYTHON_ALLOCATORS = not _gil_enabled()

_MEW_DIR = str(Path(__file__).parent)


def _caller_frame() -> Frame:
    """Return the first caller frame outside mew."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename.startswith(_MEW_DIR):
        frame = frame.f_back
    if frame is None:  # called from somewhere unexpected; keep the graph renderable
        return ("<benchmark>", "?", 0)
    return (frame.f_code.co_name, frame.f_code.co_filename, frame.f_lineno)


@dataclass(frozen=True, slots=True)
class _RootedRecord:
    """A memray allocation record rooted at its benchmark frame."""

    size: int
    n_allocations: int
    tid: int
    thread_name: str
    stack: tuple[Frame, ...]

    def stack_trace(self, max_stacks: int | None = None) -> tuple[Frame, ...]:
        return self.stack if max_stacks is None else self.stack[:max_stacks]

    def hybrid_stack_trace(self, max_stacks: int | None = None) -> tuple[Frame, ...]:
        # Only consulted under native_traces=True, which mew does not enable.
        return self.stack_trace(max_stacks)


def require_memray() -> None:
    """Raise a SystemExit with install instructions if memray is missing."""
    if find_spec("memray") is None:
        raise SystemExit(
            "memray is required for memory profiling. "
            "Install it with: uv add --optional memory memray"
        )


class MemrayManager:
    """Google Benchmark memory manager backed by memray.

    One capture per (benchmark, repetition): Google Benchmark calls
    :meth:`start`, runs the body for a small fixed iteration count outside the
    timing loop, then calls :meth:`stop`.

    Parameters
    ----------
    tmpdir : Path
        Directory for the intermediate capture files. One file per capture; the
        caller owns the directory's lifetime (see :func:`manager`).

    Notes
    -----
    Google Benchmark caps the memory pass at ``min(16, iterations)``, so
    ``allocations_per_iteration`` amortizes one-time allocations over at most 16
    calls. Compare it across engines, not across differing iteration counts.
    """

    def __init__(self, tmpdir: Path) -> None:
        self._dir = tmpdir
        self._i = 0
        self._dest: Path | None = None
        self._tracker: Tracker | None = None
        self._root: Frame = ("<benchmark>", "?", 0)
        #: Completed captures as ``(path, root_frame)``, one per (benchmark,
        #: repetition), in run order. :func:`write_flamegraph` renders them.
        self.captures: list[tuple[Path, Frame]] = []

    def start(self) -> None:
        import memray

        self._dest = self._dir / f"capture-{self._i}.bin"
        self._i += 1
        # Before entering the tracker: see _caller_frame.
        self._root = _caller_frame()
        self._tracker = memray.Tracker(self._dest, trace_python_allocators=_TRACE_PYTHON_ALLOCATORS)
        self._tracker.__enter__()

    def stop(self) -> MemoryMetrics | None:
        import memray

        tracker, dest = self._tracker, self._dest
        if tracker is None or dest is None:
            return None
        tracker.__exit__(None, None, None)
        self._tracker = None
        # Only after a clean close, so a half-written capture never reaches the
        # flame graph.
        self.captures.append((dest, self._root))
        # Metadata avoids scanning every allocation, which can take minutes for
        # an allocation-heavy body, so the optional cumulative `total_bytes`
        # metric stays unset.
        with memray.FileReader(dest) as reader:
            meta = reader.metadata
        return {
            "peak_bytes": meta.peak_memory,
            "total_allocations": meta.total_allocations,
        }


def manager(stack: ExitStack) -> MemrayManager:
    """Create a memory manager whose temporary files are owned by ``stack``.

    Parameters
    ----------
    stack : ExitStack
        Stack that keeps capture files alive through the benchmark run.

    Returns
    -------
    MemrayManager
        Manager ready to pass to :func:`mew.run`.
    """
    require_memray()
    tmpdir = stack.enter_context(tempfile.TemporaryDirectory())
    return MemrayManager(Path(tmpdir))


def write_flamegraph(manager: MemrayManager, path: Path) -> None:
    """Render ``manager``'s captures into one HTML allocation flame graph.

    Parameters
    ----------
    manager : MemrayManager
        Manager containing completed captures.
    path : Path
        Destination HTML file.
    """
    require_memray()
    import memray
    from memray.reporters.flamegraph import FlameGraphReporter

    if not manager.captures:
        print(
            "warning: no memory captures recorded; skipping flame graph "
            "(did any benchmark body enter its timing loop?)",
            file=sys.stderr,
        )
        return

    # Header metadata for the report. Every capture comes from this process, so
    # the first one speaks for all of them.
    with memray.FileReader(manager.captures[0][0]) as first:
        metadata = first.metadata

    records: list[_RootedRecord] = []
    for capture, root in manager.captures:
        with memray.FileReader(capture) as reader:
            for rec in reader.get_high_watermark_allocation_records(merge_threads=True):
                records.append(
                    _RootedRecord(
                        size=rec.size,
                        n_allocations=rec.n_allocations,
                        tid=rec.tid,
                        thread_name=rec.thread_name,
                        stack=(*rec.stack_trace(), root),
                    )
                )

    reporter = FlameGraphReporter.from_snapshot(
        # Duck-typed stand-ins for AllocationRecord; see _RootedRecord.
        cast("Any", records),
        memory_records=(),
        native_traces=False,
    )
    with path.open("w") as f:
        reporter.render(
            f,
            metadata=metadata,
            show_memory_leaks=False,
            merge_threads=True,
            inverted=False,
        )
