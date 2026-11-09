"""Memory profiling of benchmark bodies with memray."""

from __future__ import annotations

import dataclasses
import html
import shutil
import sys
import tempfile
import weakref
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from mew._typing import MemoryManager, MemoryMetrics

if TYPE_CHECKING:
    from memray import Tracker

# A memray stack frame: ``(function, filename, lineno)``.
Frame = tuple[str, str, int]


class _Capture(NamedTuple):
    """One memray capture: a benchmark's loop in one repetition of the memory pass.

    Attributes
    ----------
    path : Path
        The capture file.
    root : tuple[str, str, int]
        The benchmark body's frame, ``(function, file, line)``.
    benchmark : str
        The benchmark's registered name, ``file.py::func[label]``.
    """

    path: Path
    root: Frame
    benchmark: str


@dataclasses.dataclass(frozen=True, slots=True)
class _RootedRecord:
    """A memray allocation record with the benchmark body's frame appended as its root.

    Tracking starts in the body's first loop step, so memray never records the
    body's own frame: without the root, what the body allocates itself would sit
    on the report's ``<tracker>`` root. memray's own records resolve their stack
    through their file reader, so they can't take an extra frame.
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


class MemrayManager(MemoryManager):
    """Memory manager that tracks allocations with memray.

    It records one capture per benchmark and repetition, covering the timing
    loop of an extra, untimed, single-threaded pass of at most 16 iterations.
    ``allocations_per_iteration`` divides by the iterations that ran, which a
    batched loop can push past that cap.

    The capture files live in a temporary directory that goes with the manager.

    Raises
    ------
    ImportError
        If memray is not installed.
    """

    def __init__(self) -> None:
        if find_spec("memray") is None:
            raise ImportError(
                "memray is required for memory profiling. "
                "Install it with: uv add 'mew-bench[memory]'"
            )
        # Removed with the manager. A TemporaryDirectory would do the same, but warns
        # (ResourceWarning) when the garbage collector, not cleanup(), removes it.
        self._dir = Path(tempfile.mkdtemp(prefix="mew-memray-"))
        weakref.finalize(self, shutil.rmtree, self._dir, ignore_errors=True)
        self._i = 0
        self._dest: Path | None = None
        self._tracker: Tracker | None = None
        # Accepted captures, one per (benchmark, repetition), in run order;
        # write_outputs() renders them.
        self._captures: list[_Capture] = []

    def start(self) -> None:
        import memray

        self._dest = self._dir / f"capture-{self._i}.bin"
        self._i += 1
        from mew.api import _body_frame

        # GB calls this from the first loop step, in the body or a helper it
        # calls. Read before entering the tracker.
        body, state = _body_frame(sys._getframe(1))
        code = body.f_code
        self._root: Frame = (code.co_name, code.co_filename, body.f_lineno)
        self._benchmark: str = getattr(state, "name", None) or code.co_name
        # Python's own allocator (pymalloc, or mimalloc on free-threaded builds)
        # hands out objects from arenas it reuses, so the system allocator memray
        # hooks by default sees only new arenas: a body building 5,000 dicts per
        # iteration showed a 42 KiB peak instead of 1.4 MiB. Tracing it costs
        # about 14 ns per allocation, in a pass outside the measured repetitions.
        self._tracker = memray.Tracker(self._dest, trace_python_allocators=True)
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
        self._captures.append(_Capture(dest, self._root, self._benchmark))
        return {
            "peak_bytes": meta.peak_memory,
            "total_allocations": meta.total_allocations,
        }

    def write_outputs(self, directory: Path) -> None:
        """Write each benchmark's capture as an HTML allocation flame graph.

        The files are numbered in run order and named after the benchmark
        function, e.g. ``0-bench_sort.html``, since a case label may hold
        characters a file name can't. Each is memray's own report, rooted at the
        benchmark body, with the benchmark's full name as the command line in its
        Stats dialog; ``index.html`` links them all by that name. A benchmark's
        further repetitions, which repeat its first, are left out. When nothing
        was captured, nothing is written and a warning goes to stderr.

        Parameters
        ----------
        directory : Path
            Where to write the reports; created if missing.
        """
        if not self._captures:
            print(
                "warning: no memory captures recorded; skipping flame graphs "
                "(did any benchmark body enter its timing loop?)",
                file=sys.stderr,
            )
            return

        import memray
        from memray.reporters.flamegraph import FlameGraphReporter

        directory.mkdir(parents=True, exist_ok=True)
        firsts: dict[str, _Capture] = {}
        for capture in self._captures:
            firsts.setdefault(capture.benchmark, capture)
        captures = list(firsts.values())
        benchmarks = [(f"{i}-{c.root[0]}.html", c.benchmark) for i, c in enumerate(captures)]
        for (name, benchmark), (path, root, _) in zip(benchmarks, captures, strict=True):
            with memray.FileReader(path) as reader:
                records = [
                    _RootedRecord(
                        size=rec.size,
                        n_allocations=rec.n_allocations,
                        tid=rec.tid,
                        thread_name=rec.thread_name,
                        stack=(*rec.stack_trace(), root),
                    )
                    for rec in reader.get_high_watermark_allocation_records(merge_threads=True)
                ]
                reporter = FlameGraphReporter.from_snapshot(
                    # Duck-typed stand-ins for AllocationRecord; see _RootedRecord.
                    cast("Any", records),
                    memory_records=tuple(reader.get_memory_snapshots()),
                    native_traces=False,
                )
                # The header's command line names the benchmark instead.
                metadata = dataclasses.replace(reader.metadata, command_line=benchmark)
            with (directory / name).open("w", encoding="utf-8") as fh:
                reporter.render(
                    fh, metadata, show_memory_leaks=False, merge_threads=True, inverted=False
                )
        links = "".join(
            f'<li><a href="{html.escape(name)}">{html.escape(benchmark)}</a></li>\n'
            for name, benchmark in benchmarks
        )
        (directory / "index.html").write_text(
            '<!doctype html>\n<meta charset="utf-8">\n<title>Memory flame graphs</title>\n'
            f"<h1>Memory flame graphs</h1>\n<ul>\n{links}</ul>\n",
            encoding="utf-8",
        )
