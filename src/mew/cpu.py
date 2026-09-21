"""CPU profiling with pyinstrument as a Google Benchmark profiler manager.

Google Benchmark runs one extra, untimed pass per repetition with the sampler
active from the first ``for _ in state`` to loop exit. The summary from
:meth:`PyinstrumentManager.get_result` reaches reporters as the ``cpu_profile``
block of a :class:`~mew._typing.BenchmarkResult`.
"""

from __future__ import annotations

from importlib.util import find_spec
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING

from mew._typing import ProfilerSummary
from mew.machine import _gil_enabled

if TYPE_CHECKING:
    from pyinstrument import Profiler
    from pyinstrument.frame import Frame
    from pyinstrument.session import Session


def require_pyinstrument() -> None:
    """Check that pyinstrument is installed and safe for this interpreter."""
    if not _gil_enabled():
        raise SystemExit(
            "pyinstrument does not support free-threaded Python: importing its "
            "native sampler would enable the GIL"
        )
    if find_spec("pyinstrument") is None:
        raise SystemExit(
            "pyinstrument is required for CPU profiling. "
            "Install it with: uv add --optional cpu pyinstrument"
        )


class PyinstrumentManager:
    """Google Benchmark profiler manager backed by pyinstrument.

    Sampling is scoped to the timing loop, so fixture/setup work is excluded, and
    it runs outside the measured repetitions, so the figures never perturb the
    reported times. ``state.pause()`` regions are excluded as well: the binding
    calls :meth:`pause` / :meth:`resume` around them.

    Parameters
    ----------
    interval : float, default 1e-4
        Pyinstrument's sampling period in seconds.

    Attributes
    ----------
    sessions : list[Session]
        Every session captured, kept only so :func:`write_html` can render one
        combined report; the per-row summaries ride on the ``Run``.

    Raises
    ------
    ValueError
        If ``interval`` is not positive and finite.
    SystemExit
        If pyinstrument is missing or the interpreter is free-threaded.
    """

    def __init__(self, interval: float = 1e-4) -> None:
        if not isfinite(interval) or interval <= 0:
            raise ValueError(f"interval must be a positive finite number, got {interval!r}")
        require_pyinstrument()
        self._interval = interval
        self._prof: Profiler | None = None
        self._session: Session | None = None
        self._depth = 0
        self.sessions: list[Session] = []

    def after_setup_start(self) -> None:
        import pyinstrument

        self._session = None
        self._prof = pyinstrument.Profiler(interval=self._interval, async_mode="disabled")
        self._prof.start()

    def before_teardown_stop(self) -> None:
        if self._prof is None:
            return
        self._prof.stop()
        self._session = self._prof.last_session
        self._prof = None
        if self._session is not None:
            self.sessions.append(self._session)

    def pause(self) -> None:
        """Suspend sampling for a ``state.pause()`` region.

        pyinstrument accumulates across ``stop()``/``start()`` and drops the gap.
        Only the outermost pause toggles it: unbalanced start/stop raises.
        """
        if self._prof is not None and self._depth == 0:
            self._prof.stop()
        self._depth += 1

    def resume(self) -> None:
        self._depth -= 1
        if self._prof is not None and self._depth == 0:
            self._prof.start()

    def get_result(self) -> ProfilerSummary | None:
        """Summarize the last session, or ``None`` when nothing was sampled.

        A body too fast for the interval would otherwise report a
        ``<no samples>`` hottest frame on every row.
        """
        session = self._session
        if session is None or session.sample_count == 0:
            return None
        root = session.root_frame()
        if root is None:
            return None
        where, self_time = _hottest_frame(root)
        return {
            "profiler": "pyinstrument",
            "wall_time": session.duration,
            "sample_count": session.sample_count,
            "top_function": where,
            "top_function_total_self_time": self_time,
        }


def _hottest_frame(root: Frame) -> tuple[str, float]:
    """Return ``("func (file.py:12)", self_seconds)`` for the hottest user call site.

    Summed per call site, not per frame: pyinstrument records one frame per
    *call*, so a helper invoked N times holds 1/N of the time in each of N
    siblings while the calling loop accumulates in one. Picking the largest
    single frame would name the loop, and flip between runs with sample
    coalescing. ``[self]`` frames are synthetic leaves already summed into their
    parent's ``total_self_time``, so they are skipped to avoid double counting.
    """
    totals: dict[tuple[str, str, int | None], float] = {}
    stack = [root]
    while stack:
        f = stack.pop()
        stack.extend(f.children)
        path = Path(f.file_path) if f.file_path else None
        if f.is_synthetic or (path is not None and "pyinstrument" in path.parts):
            continue
        file_name = path.name if path is not None else "?"
        key = (f.function, file_name, f.line_no)
        totals[key] = totals.get(key, 0.0) + f.total_self_time
    if not totals:
        return "<no samples>", 0.0
    (function, file_name, line_no), self_time = max(totals.items(), key=lambda kv: kv[1])
    return f"{function} ({file_name}:{line_no})", self_time


def write_html(sessions: list[Session], path: Path) -> None:
    """Render captured sessions as one pyinstrument HTML report.

    Parameters
    ----------
    sessions : list[Session]
        Sessions to combine.
    path : Path
        Destination HTML file.
    """
    from functools import reduce

    from pyinstrument.renderers import HTMLRenderer
    from pyinstrument.session import Session

    if not sessions:
        return
    combined = reduce(Session.combine, sessions)
    path.write_text(HTMLRenderer().render(combined))
