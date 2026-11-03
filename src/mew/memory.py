"""Memory profiling of benchmark bodies with memray."""

from __future__ import annotations

import dataclasses
import html
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from mew._typing import MemoryManager, MemoryMetrics
from mew.machine import is_free_threaded_build

if TYPE_CHECKING:
    from memray import Tracker

# A memray stack frame: ``(function, filename, lineno)``.
Frame = tuple[str, str, int]

# memray's flame graph page with a benchmark picker in its navbar. memray's
# template environment doesn't autoescape, hence the explicit `|e`.
_FLAMEGRAPH_TEMPLATE = """{% extends "flamegraph.html" %}
{% block topbar_buttons %}
<select class="custom-select custom-select-sm mr-3 w-auto" aria-label="Benchmark"
        onchange="window.location.href = this.value">
  {%- for file, name in benchmarks %}
  <option value="{{ file|e }}"{% if file == current %} selected{% endif %}>{{ name|e }}</option>
  {%- endfor %}
</select>
{{ super() }}
{% endblock topbar_buttons %}
"""

# Free-threaded CPython serves object allocations from mimalloc, which never
# reaches the system allocator memray hooks by default, so track them separately.
_TRACE_PYTHON_ALLOCATORS = is_free_threaded_build()


class Capture(NamedTuple):
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
    loop of an extra, untimed, single-threaded pass of at most
    ``memory_iterations`` iterations (see :func:`mew.run`).
    ``allocations_per_iteration`` divides by the iterations that ran, which a
    batched loop can push past that cap.

    The capture files live in a temporary directory that goes with the manager.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mew-memray-")
        self._dir = Path(self._tmp.name)
        self._i = 0
        self._dest: Path | None = None
        self._tracker: Tracker | None = None
        #: Accepted captures, one per (benchmark, repetition), in run order.
        #: :meth:`write_outputs` renders them.
        self.captures: list[Capture] = []

    def start(self) -> None:
        import memray

        self._dest = self._dir / f"capture-{self._i}.bin"
        self._i += 1
        # GB calls this from the body's first loop step, so the direct Python
        # caller is the benchmark body. Read before entering the tracker.
        body = sys._getframe(1)
        code = body.f_code
        self._root: Frame = (code.co_name, code.co_filename, body.f_lineno)
        # mew passes the State first, whatever the parameter's name.
        state = body.f_locals.get(code.co_varnames[0]) if code.co_argcount else None
        self._benchmark: str = getattr(state, "name", None) or code.co_name
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
        self.captures.append(Capture(dest, self._root, self._benchmark))
        return {
            "peak_bytes": meta.peak_memory,
            "total_allocations": meta.total_allocations,
        }

    def write_outputs(self, directory: Path) -> None:
        """Write each benchmark's capture as an HTML allocation flame graph.

        The files are numbered in run order and named after the benchmark
        function, e.g. ``0-bench_sort.html``, since a case label may hold
        characters a file name can't. Each is memray's report with a picker
        for the other benchmarks, by name, in its navbar; ``index.html`` opens
        the first. A benchmark's further repetitions, which repeat its first,
        are left out. When nothing was captured, nothing is written and a
        warning goes to stderr.

        Parameters
        ----------
        directory : Path
            Where to write the reports; created if missing.
        """
        if not self.captures:
            print(
                "warning: no memory captures recorded; skipping flame graphs "
                "(did any benchmark body enter its timing loop?)",
                file=sys.stderr,
            )
            return

        import memray
        from memray.reporters.flamegraph import FlameGraphReporter
        from memray.reporters.templates import get_render_environment

        directory.mkdir(parents=True, exist_ok=True)
        firsts: dict[str, Capture] = {}
        for capture in self.captures:
            firsts.setdefault(capture.benchmark, capture)
        captures = list(firsts.values())
        benchmarks = [(f"{i}-{c.root[0]}.html", c.benchmark) for i, c in enumerate(captures)]
        template = get_render_environment().from_string(_FLAMEGRAPH_TEMPLATE)
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
            # What FlameGraphReporter.render passes the template, plus the picker's.
            page = template.render(
                kind="flamegraph",
                title="flamegraph report",
                data=reporter.data,
                metadata=metadata,
                memory_records=reporter.memory_records,
                show_memory_leaks=False,
                merge_threads=True,
                inverted=False,
                no_web=False,
                benchmarks=benchmarks,
                current=name,
            )
            (directory / name).write_text(page, encoding="utf-8")
        first = html.escape(benchmarks[0][0])
        (directory / "index.html").write_text(
            f'<!doctype html>\n<meta charset="utf-8">\n'
            f'<meta http-equiv="refresh" content="0; url={first}">\n'
            f'<title>Memory flame graphs</title>\n<a href="{first}">Memory flame graphs</a>\n',
            encoding="utf-8",
        )


def manager() -> MemrayManager:
    """Create a memray manager, checking that memray is installed.

    Returns
    -------
    MemrayManager
        A manager to pass to :func:`mew.run`.

    Raises
    ------
    ImportError
        If memray is not installed.
    """
    if find_spec("memray") is None:
        raise ImportError(
            "memray is required for memory profiling. Install it with: uv add 'mew-bench[memory]'"
        )
    return MemrayManager()
