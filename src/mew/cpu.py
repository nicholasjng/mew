"""CPU sampling profilers for benchmark bodies.

Each sampler is a profiler manager: Google Benchmark runs one extra, untimed
pass per repetition with the sampler active from the first ``for _ in state``
to loop exit. :func:`manager` picks the
sampler: Tachyon (:mod:`profiling.sampling`) on Python 3.15+, pyinstrument
before. Its ``get_result()`` summary reaches reporters as the ``cpu_profile``
block of a :class:`mew.BenchmarkResult`, a :class:`CpuProfile` with the
same keys and types whichever sampler and interpreter wrote it.
"""

from __future__ import annotations

import contextlib
import functools
import io
import linecache
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from importlib.util import find_spec
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from mew._typing import PausableProfiler, ProfilerManager
from mew.machine import gil_enabled

if TYPE_CHECKING:
    from pyinstrument import Profiler
    from pyinstrument.frame import Frame
    from pyinstrument.session import Session


class TopFunction(TypedDict):
    """The function with the most self time.

    Attributes
    ----------
    function : str
        Its name, or a synthetic frame's such as ``<GC>``.
    file : str or None
        Its file's name; ``None`` for a synthetic frame.
    line : int or None
        Its line with the most time; ``None`` under pyinstrument, which sees
        lines only where a call or return happened.
    self_time : float
        Seconds spent in the function itself, not in what it called.
    """

    function: str
    file: str | None
    line: int | None
    self_time: float


class TopInstruction(TypedDict):
    """A bytecode instruction among the hottest of a body.

    Attributes
    ----------
    instruction : str
        Its name, specialized forms included, e.g. ``"BINARY_OP_ADD_INT"``.
    source : str
        The code it covers, cut to its first line and 40 characters.
    file : str
        Its file's name.
    line : int
        Its first line.
    share : float
        Its share of the body's time, from 0 to 1.
    unspecialized : bool
        Whether it is generic though the interpreter can specialize it.
    """

    instruction: str
    source: str
    file: str
    line: int
    share: float
    unspecialized: bool


class CpuProfile(TypedDict):
    """A CPU sampler's summary of one profiling pass.

    Every sampler writes the same keys and types: a figure the sampler can't
    measure is ``None``, a list it can't measure empty, so archives mixing
    interpreters keep one column type.

    Attributes
    ----------
    profiler : str
        ``"tachyon"`` or ``"pyinstrument"``.
    wall_time : float
        Seconds sampled: the timing loop minus ``state.pause()`` regions.
    sample_count : int
        Samples taken, 0 for a pass shorter than the sampling interval. Each
        sampler counts differently, so compare ``wall_time`` and ``self_time``
        across samplers instead.
    top_function : TopFunction or None
        The function with the most self time; ``None`` when no sample caught
        the body running, as in a pass shorter than the sampling interval.
    top_instructions : list of TopInstruction
        The ``top_k`` hottest bytecode instructions, hottest first; empty
        unless :func:`reads_instructions`.
    unspecialized_share : float or None
        Share of the body's time in instructions the interpreter can
        specialize but didn't; ``None`` unless :func:`reads_instructions`.
    """

    profiler: str
    wall_time: float
    sample_count: int
    top_function: TopFunction | None
    top_instructions: list[TopInstruction]
    unspecialized_share: float | None


#: The samplers :func:`manager` accepts.
SAMPLERS = ("auto", "tachyon", "pyinstrument")


def manager(
    interval: float = 1e-4, *, sampler: str = "auto", top_k: int = 5
) -> TachyonManager | PyinstrumentManager:
    """Create the profiler manager for a CPU sampler.

    Parameters
    ----------
    interval : float, default 1e-4
        Sampling period in seconds.
    sampler : {"auto", "tachyon", "pyinstrument"}, default "auto"
        ``"auto"`` picks Tachyon on Python 3.15+ and pyinstrument before. Pick
        pyinstrument on 3.15+ to profile a suite with one sampler across
        interpreter versions; free-threaded builds need Tachyon.
    top_k : int, default 5
        How many of the hottest bytecode instructions Tachyon reports where
        :func:`reads_instructions`. pyinstrument reads no instructions, but
        ``top_k`` is checked for it too, so a bad value fails on every
        interpreter.

    Returns
    -------
    TachyonManager or PyinstrumentManager
        The sampler's manager.

    Raises
    ------
    ValueError
        If ``sampler`` is none of the three above, ``interval`` is not positive
        and finite, or ``top_k`` is not an integer of at least 1. All three are
        checked before the sampler is chosen.
    ImportError
        If the sampler is pyinstrument and it is not installed.
    RuntimeError
        If the sampler can't run here: Tachyon before Python 3.15 or in a
        process it cannot read, pyinstrument on a free-threaded build.
    """
    if sampler not in SAMPLERS:
        raise ValueError(f"sampler must be one of {', '.join(SAMPLERS)}, got {sampler!r}")
    _check_interval(interval)
    _check_top_k(top_k)
    if sampler == "auto":
        sampler = "tachyon" if sys.version_info >= (3, 15) else "pyinstrument"
    if sampler == "tachyon":
        return TachyonManager(interval, top_k=top_k)
    return PyinstrumentManager(interval)


def _check_interval(interval: float) -> None:
    if not isfinite(interval) or interval <= 0:
        raise ValueError(f"interval must be a positive finite number, got {interval!r}")


def _check_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"top_k must be an integer >= 1, got {top_k!r}")


class PyinstrumentManager(ProfilerManager, PausableProfiler):
    """Profiler manager that samples with pyinstrument.

    Sampling covers the timing loop minus ``state.pause()`` regions, in a pass
    outside the measured repetitions, so it never changes the reported times.

    Parameters
    ----------
    interval : float, default 1e-4
        Sampling period in seconds.

    Attributes
    ----------
    sessions : list[Session]
        Every pass's session, for :func:`write_html`.

    Raises
    ------
    ValueError
        If ``interval`` is not positive and finite.
    RuntimeError
        If the interpreter is free-threaded.
    ImportError
        If pyinstrument is not installed.
    """

    def __init__(self, interval: float = 1e-4) -> None:
        _check_interval(interval)
        if not gil_enabled():
            raise RuntimeError(
                "pyinstrument does not support free-threaded Python: importing its "
                "native sampler would enable the GIL. On Python 3.15+, mew samples "
                "with Tachyon instead."
            )
        if find_spec("pyinstrument") is None:
            raise ImportError(
                "pyinstrument is required for CPU profiling. "
                "Install it with: uv add 'mew-bench[cpu]'"
            )
        self._interval = interval
        self._prof: Profiler | None = None
        self._session: Session | None = None
        self.sessions: list[Session] = []

    def after_setup_start(self) -> None:
        import pyinstrument

        self._session = None
        self._prof = pyinstrument.Profiler(interval=self._interval, async_mode="disabled")
        self._prof.start()

    def before_teardown_stop(self) -> None:
        if self._prof is None:
            return
        self._session = self._prof.stop()
        self.sessions.append(self._session)
        self._prof = None

    def pause(self) -> None:
        """Suspend sampling.

        Called when the body enters a ``state.pause()`` scope.
        """
        # The session continues across stop() and start(), without the gap.
        if self._prof is not None:
            self._prof.stop()

    def resume(self) -> None:
        if self._prof is not None:
            self._prof.start()

    def get_result(self) -> CpuProfile | None:
        """Summarize the pass that just ended.

        Returns
        -------
        CpuProfile or None
            The summary, or ``None`` if no pass started. A pass without samples
            gets one with ``sample_count`` 0 and no ``top_function``.
        """
        # Taken, so a pass that skips before its loop can't report the last one's.
        session, self._session = self._session, None
        if session is None:
            return None
        root = session.root_frame()
        return {
            "profiler": "pyinstrument",
            "wall_time": session.duration,
            "sample_count": session.sample_count,
            "top_function": None if root is None else _hottest_frame(root),
            "top_instructions": [],
            "unspecialized_share": None,
        }


def _hottest_frame(root: Frame) -> TopFunction | None:
    """The user function with the most self time; ``None`` if there is none.

    Summed per function: pyinstrument records one frame per *call*, so the
    largest single frame would name the calling loop instead of a helper it calls
    N times. Synthetic ``[self]`` frames are skipped; their parent already counts
    them. Its ``line`` stays ``None``: pyinstrument's is the ``def`` line.
    """
    totals: dict[tuple[str, str | None, int | None], float] = {}
    stack = [root]
    while stack:
        f = stack.pop()
        stack.extend(f.children)
        path = Path(f.file_path) if f.file_path else None
        if f.is_synthetic or (path is not None and "pyinstrument" in path.parts):
            continue
        # The def line keeps apart same-named functions in one file.
        key = (f.function, path.name if path is not None else None, f.line_no)
        totals[key] = totals.get(key, 0.0) + f.total_self_time
    if not totals:
        return None
    (function, file_name, _), self_time = max(totals.items(), key=lambda kv: kv[1])
    return {"function": function, "file": file_name, "line": None, "self_time": self_time}


def _require_tachyon() -> None:
    """Raise :class:`RuntimeError` before Python 3.15, which has no Tachyon."""
    # Not inlined: type checkers would take the code after the check as unreachable.
    if sys.version_info < (3, 15):
        raise RuntimeError("Tachyon CPU sampling requires Python 3.15 or newer")


def reads_instructions() -> bool:
    """Return whether Tachyon reads bytecode instructions in this interpreter.

    It does on free-threaded builds of Python 3.15+. A GIL build is sampled
    only where the body hands over the GIL, so every sample would name a call
    or a loop jump.

    Returns
    -------
    bool
        ``True`` on Python 3.15 or newer with the GIL disabled.
    """
    return sys.version_info >= (3, 15) and not gil_enabled()


@functools.cache
def _unwinder(pid: int) -> Any:
    """Return the unwinder that reads process ``pid``, created once per process.

    On macOS, creating one fails after memray has tracked the process (3.15.0,
    memray 1.20).

    ``<native>`` frames mark where C code calls back into Python.
    """
    import _remote_debugging

    return _remote_debugging.RemoteUnwinder(
        pid,
        all_threads=True,
        gc=True,
        native=True,
        opcodes=reads_instructions(),
        cache_frames=True,
    )


@functools.cache
def _opcode_tables() -> tuple[dict[int, str], frozenset[int]] | None:
    """Return every opcode's name and the set of opcodes the interpreter can specialize.

    Names include the specialized forms. ``None`` if these undocumented tables moved.
    """
    import opcode

    try:
        names = dict(enumerate(opcode.opname))
        specialized = opcode._specialized_opmap  # ty: ignore[unresolved-attribute]
        names.update({op: name for name, op in specialized.items()})
        families = opcode._specializations  # ty: ignore[unresolved-attribute]
        generic = frozenset(opcode.opmap[name] for name in families)
    except (AttributeError, KeyError):
        return None
    return names, generic


def _instruction_source(file: str, location: tuple[int, int, int, int]) -> str:
    """The source an instruction covers, cut to its first line and 40 characters."""
    line, end_line, col, end_col = location
    text = linecache.getline(file, line)
    if col < 0:  # No column positions, e.g. under -X no_debug_ranges.
        source = text.strip()
    else:
        # Columns count UTF-8 bytes, not characters.
        end = end_col if end_line == line else None
        source = text.encode()[col:end].decode(errors="replace").strip()
        if end is None:
            source += " …"
    return source if len(source) <= 40 else source[:39] + "…"


def _hottest_function(
    lines: dict[tuple[str, str, int], float], wall_time: float
) -> TopFunction | None:
    """The function with the most self time, at its hottest line; ``None`` if there is none.

    ``lines`` maps ``(file, function, line)`` to self time.
    """
    if not (total := sum(lines.values())):
        return None
    functions: defaultdict[tuple[str, str], float] = defaultdict(float)
    for (file, func, _), seconds in lines.items():
        functions[file, func] += seconds
    (file, func), hot = max(functions.items(), key=lambda kv: kv[1])
    line = max((k for k in lines if k[:2] == (file, func)), key=lines.__getitem__)[2]
    # Synthetic frames such as <GC> have no source file.
    synthetic = file == "~"
    return {
        "function": func,
        "file": None if synthetic else Path(file).name,
        "line": None if synthetic else line,
        # Scaled to the unpaused time, which also covers the stretch after the
        # last sample. The share first: a function holding every sample must get
        # exactly wall_time, not an ulp above.
        "self_time": wall_time * (hot / total),
    }


def _summarize_instructions(
    instructions: dict[tuple[str, Any, int], float], total: float, top_k: int
) -> tuple[list[TopInstruction], float | None]:
    """The ``top_k`` hottest instructions and the share of time in unspecialized ones.

    ``instructions`` maps ``(file, location, opcode)`` to self time; ``total`` is
    the body's self time, so shares compare with the hottest function's. Without
    instructions or time, or if the opcode tables moved, ``([], None)``.
    """
    if not instructions or not total or (tables := _opcode_tables()) is None:
        return [], None
    names, generic = tables
    hottest = sorted(instructions.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    unspecialized = sum(t for (_, _, op), t in instructions.items() if op in generic)
    top: list[TopInstruction] = [
        {
            "instruction": names.get(op, f"<{op}>"),
            "source": _instruction_source(file, location),
            "file": Path(file).name,
            "line": location[0],
            "share": seconds / total,
            "unspecialized": op in generic,
        }
        for (file, location, op), seconds in hottest
    ]
    return top, unspecialized / total


class TachyonManager(ProfilerManager, PausableProfiler):
    """Profiler manager that samples with Tachyon (:mod:`profiling.sampling`).

    A background thread samples the benchmark's thread by reading this process's
    memory, so the body runs uninstrumented. Sampling covers the timing loop
    minus ``state.pause()`` regions, in a pass outside the measured repetitions.

    Free-threaded builds sample beside the body. On a GIL build every sample
    needs the GIL, so the pass lowers the switch interval to ``interval``; the
    body still hands the GIL over only where the interpreter checks for a
    switch, such as a loop jump. A C call that holds the GIL thus delays the
    next sample until it returns, so each sample counts as the time since the
    previous one, and a short Python callback inside such a call can go
    unsampled.

    Parameters
    ----------
    interval : float, default 1e-4
        Sampling period in seconds.
    top_k : int, default 5
        How many of the hottest bytecode instructions to report where
        :func:`reads_instructions`.

    Attributes
    ----------
    flamegraph : profiling.sampling.stack_collector.FlamegraphCollector
        Every pass's samples, cut at the benchmark body, for :func:`write_html`.
        Its stacks carry ``<native>`` frames and, on free-threaded builds, each
        frame's bytecode instructions.
    reads_instructions : bool
        Whether summaries list the hottest bytecode instructions; see
        :func:`reads_instructions`.

    Raises
    ------
    ValueError
        If ``interval`` is not positive and finite, or ``top_k`` is not an
        integer of at least 1.
    RuntimeError
        Before Python 3.15, or if the process cannot read its own memory.
    """

    def __init__(self, interval: float = 1e-4, *, top_k: int = 5) -> None:
        _check_interval(interval)
        _check_top_k(top_k)
        _require_tachyon()

        from profiling.sampling.stack_collector import FlamegraphCollector

        try:
            self._unwinder = _unwinder(os.getpid())
        except (OSError, RuntimeError) as e:
            raise RuntimeError(f"Tachyon cannot sample this process: {e}") from e
        self._interval = interval
        self._top_k = top_k
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = False
        self._error: BaseException | None = None
        # The current pass: the body's thread and code, its stacks' sample counts
        # and the seconds those samples stand for, its unpaused time.
        self._tid = 0
        self._body = ("", "")
        self._stacks: Counter[tuple[int, tuple[Any, ...]]] = Counter()
        self._seconds: defaultdict[tuple[int, tuple[Any, ...]], float] = defaultdict(float)
        self._active = 0.0
        self._since = 0.0
        self._result: CpuProfile | None = None
        self._switch_interval: float | None = None
        # All passes, for the flame graph's header.
        self._samples = 0
        self._duration = 0.0
        self.flamegraph = FlamegraphCollector(round(interval * 1e6))
        self.reads_instructions = reads_instructions()

    def after_setup_start(self) -> None:
        # GB calls this from the body's first loop step, so the direct Python
        # caller is the benchmark body.
        body = sys._getframe(1).f_code
        self._body = (body.co_filename, body.co_qualname)
        self._tid = threading.get_native_id()
        self._stacks = Counter()
        self._seconds = defaultdict(float)
        self._result = None
        self._active = 0.0
        if gil_enabled():
            self._switch_interval = sys.getswitchinterval()
            sys.setswitchinterval(min(self._interval, self._switch_interval))
        self._since = time.perf_counter()
        self._running = True
        self._thread = threading.Thread(target=self._sample, name="mew-tachyon", daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        unwinder, tid = self._unwinder, self._tid
        stacks, seconds = self._stacks, self._seconds
        next_sample = last = time.perf_counter()
        try:
            while self._running:
                if not self._paused:
                    taken = time.perf_counter()
                    try:
                        interpreters = unwinder.get_stack_trace()
                    except (OSError, RuntimeError, UnicodeDecodeError, MemoryError):
                        pass  # A read that raced a changing stack; Tachyon drops these too.
                    else:
                        for interpreter in interpreters:
                            for thread in interpreter.threads:
                                if thread.thread_id == tid and thread.frame_info:
                                    key = thread.status, tuple(thread.frame_info)
                                    stacks[key] += 1
                                    # Since the last sample or resume. A failed read
                                    # leaves `last`, so its time goes to the next one.
                                    seconds[key] += taken - max(last, self._since)
                                    last = taken
                now = time.perf_counter()
                next_sample = max(next_sample + self._interval, now)
                time.sleep(next_sample - now)
        except BaseException as e:
            self._error = e

    def before_teardown_stop(self) -> None:
        if self._thread is None:
            return
        self._active += time.perf_counter() - self._since
        self._running = False
        self._thread.join()
        self._thread = None
        if self._switch_interval is not None:
            sys.setswitchinterval(self._switch_interval)
            self._switch_interval = None
        if (error := self._error) is not None:
            self._error = None
            raise error
        self._record()

    def pause(self) -> None:
        """Suspend sampling.

        Called when the body enters a ``state.pause()`` scope.
        """
        if self._thread is not None:
            self._active += time.perf_counter() - self._since
            self._paused = True

    def resume(self) -> None:
        if self._thread is not None:
            self._since = time.perf_counter()
            self._paused = False

    def get_result(self) -> CpuProfile | None:
        """Summarize the pass that just ended.

        Returns
        -------
        CpuProfile or None
            The summary, or ``None`` if no pass started. A pass without samples
            gets one with ``sample_count`` 0 and no ``top_function``.
        """
        # Taken, so a pass that skips before its loop can't report the last one's.
        result, self._result = self._result, None
        return result

    def _record(self) -> None:
        """Summarize the pass and add its stacks to the flame graph."""
        from _remote_debugging import InterpreterInfo, ThreadInfo
        from profiling.sampling.constants import PROFILING_MODE_WALL

        samples = 0
        lines: defaultdict[tuple[str, str, int], float] = defaultdict(float)
        # Keyed by (file, location, opcode); opcodes are read on free-threaded builds.
        instructions: defaultdict[tuple[str, Any, int], float] = defaultdict(float)
        for (status, frames), n in self._stacks.items():
            seconds = self._seconds[status, frames]
            frames = self._cut_at_body(frames)
            # The body thread inside this manager's hooks, e.g. starting the sampler.
            if any(frame.filename == __file__ for frame in frames):
                continue
            samples += n
            leaf = frames[0]
            lines[leaf.filename, leaf.funcname, leaf.location[0] if leaf.location else 0] += seconds
            if leaf.opcode is not None and leaf.location:
                instructions[leaf.filename, leaf.location, leaf.opcode] += seconds
            # The flame graph draws a count times the interval as time, so it gets
            # the seconds in intervals rather than the sample count.
            if intervals := round(seconds / self._interval):
                thread = ThreadInfo((self._tid, status, list(frames)))
                self.flamegraph.collect(
                    [InterpreterInfo((0, [thread]))], timestamps_us=[0] * intervals
                )
        self._samples += samples
        self._duration += self._active
        if self._duration > 0:
            rate = self._samples / self._duration
            interval_us = round(self._interval * 1e6)
            self.flamegraph.set_stats(interval_us, self._duration, rate, mode=PROFILING_MODE_WALL)
        top, unspecialized = _summarize_instructions(instructions, sum(lines.values()), self._top_k)
        self._result = {
            "profiler": "tachyon",
            "wall_time": self._active,
            "sample_count": samples,
            "top_function": _hottest_function(lines, self._active),
            "top_instructions": top,
            "unspecialized_share": unspecialized,
        }

    def _cut_at_body(self, frames: tuple[Any, ...]) -> tuple[Any, ...]:
        """Drop the frames that called the benchmark body; ``frames`` run leaf first."""
        for i in range(len(frames) - 1, -1, -1):
            if (frames[i].filename, frames[i].funcname) == self._body:
                return frames[: i + 1]
        return frames


def write_html(manager: TachyonManager | PyinstrumentManager, path: Path) -> None:
    """Render ``manager``'s profiling passes as one HTML report.

    Tachyon writes a flame graph with one root per benchmark, pyinstrument one
    combined call tree. When no pass collected samples, nothing is written and
    a warning goes to stderr.

    Parameters
    ----------
    manager : TachyonManager or PyinstrumentManager
        Manager whose passes have completed.
    path : Path
        Destination HTML file.

    Raises
    ------
    TypeError
        If ``manager`` is neither of the two, such as a custom profiler manager.
    """
    if not isinstance(manager, TachyonManager | PyinstrumentManager):
        raise TypeError(
            "write_html needs a TachyonManager or PyinstrumentManager, "
            f"got {type(manager).__name__}"
        )
    if isinstance(manager, TachyonManager):
        # export() reports progress on stdout, which may carry a JSON result
        # stream, and returns whether it wrote the file.
        with contextlib.redirect_stdout(io.StringIO()):
            written = manager.flamegraph.export(str(path))
    elif written := any(session.sample_count for session in manager.sessions):
        from pyinstrument.renderers import HTMLRenderer
        from pyinstrument.session import Session

        combined = functools.reduce(Session.combine, manager.sessions)
        path.write_text(HTMLRenderer().render(combined), encoding="utf-8")
    if not written:
        print(
            "warning: no CPU samples collected; skipping the HTML report "
            "(are the profiled passes shorter than the sampling interval?)",
            file=sys.stderr,
        )
