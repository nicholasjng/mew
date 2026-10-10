"""Memory profiling with memray and Google Benchmark's memory manager."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from contextlib import ExitStack
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


@dataclasses.dataclass(frozen=True, slots=True)
class _RootedRecord:
    """A memray allocation record with the benchmark frame appended as its root.

    Tracking starts inside the benchmark body, so memray never sees the body's
    own frame; the root lets a combined flame graph tell captures apart.
    """

    size: int
    n_allocations: int
    tid: int
    thread_name: str
    stack: tuple[Frame, ...]

    def stack_trace(self) -> tuple[Frame, ...]:
        # The flame graph calls this without arguments; `hybrid_stack_trace`
        # is only consulted under native_traces=True, which mew does not enable.
        return self.stack


def require_memray() -> None:
    """Raise a SystemExit with install instructions if memray is missing."""
    if find_spec("memray") is None:
        raise SystemExit(
            "memray is required for memory profiling. "
            "Install it with: uv add --optional memory memray"
        )


class MemrayManager:
    """Google Benchmark memory manager backed by memray.

    One capture per (benchmark, repetition), scoped to the benchmark loop in
    a separate, untimed pass. :meth:`start` opens the tracker and :meth:`stop`
    closes it and records the capture.

    Parameters
    ----------
    tmpdir : Path
        Directory for the intermediate capture files. One file per capture; the
        caller owns the directory's lifetime (see :func:`manager`).

    Notes
    -----
    The memory pass runs a single thread and requests
    ``min(memory_iterations, iterations)`` iterations (16 by default; see
    :func:`mew.run`). Batched loops can exceed this budget;
    ``allocations_per_iteration`` uses the actual count.
    """

    def __init__(self, tmpdir: Path) -> None:
        self._dir = tmpdir
        self._i = 0
        self._dest: Path | None = None
        self._tracker: Tracker | None = None
        #: Accepted captures as ``(path, root_frame)``, one per (benchmark,
        #: repetition), in run order. :func:`write_flamegraph` renders them.
        self.captures: list[tuple[Path, Frame]] = []

    def start(self) -> None:
        import memray

        self._dest = self._dir / f"capture-{self._i}.bin"
        self._i += 1
        # GB calls this from the body's first loop step, so the direct Python
        # caller is the benchmark body. Read before entering the tracker.
        body = sys._getframe(1)
        self._root: Frame = (body.f_code.co_name, body.f_code.co_filename, body.f_lineno)
        self._tracker = memray.Tracker(self._dest, trace_python_allocators=_TRACE_PYTHON_ALLOCATORS)
        self._tracker.__enter__()

    def stop(self) -> MemoryMetrics | None:
        import memray

        tracker, dest = self._tracker, self._dest
        # GB pairs this with start() even when start() raised before tracking.
        if tracker is None or dest is None:
            return None
        tracker.__exit__(None, None, None)
        self._tracker = None
        # Metadata avoids scanning every allocation (minutes for heavy bodies),
        # so the optional `total_bytes` metric stays unset.
        with memray.FileReader(dest) as reader:
            meta = reader.metadata
        self.captures.append((dest, self._root))
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
    if not manager.captures:
        print(
            "warning: no memory captures recorded; skipping flame graph "
            "(did any benchmark body enter its timing loop?)",
            file=sys.stderr,
        )
        return

    import memray
    from memray.reporters.flamegraph import FlameGraphReporter

    metas = []
    records: list[_RootedRecord] = []
    for capture, root in manager.captures:
        with memray.FileReader(capture) as reader:
            metas.append(reader.metadata)
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

    # Process fields are shared; the figures must cover every capture.
    metadata = dataclasses.replace(
        metas[0],
        start_time=min(m.start_time for m in metas),
        end_time=max(m.end_time for m in metas),
        total_allocations=sum(m.total_allocations for m in metas),
        peak_memory=max(m.peak_memory for m in metas),
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
