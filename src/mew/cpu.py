"""Optional CPU profiling via pyinstrument, wired in as a Google Benchmark profiler manager.

Google Benchmark drives the sampling itself: when a profiler manager is
registered it runs one extra, untimed pass of each benchmark body per
repetition, starting the sampler at the first ``for _ in state`` (after fixture
setup) and stopping it at loop exit. The summary :meth:`PyinstrumentManager.get_result`
returns is stamped onto that repetition's ``Run`` and reaches reporters as the
``cpu_profile`` block of a :class:`~mew._typing.RunRow`.
"""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyinstrument import Profiler
    from pyinstrument.frame import Frame
    from pyinstrument.session import Session


def require_pyinstrument() -> None:
    """Raise a SystemExit with install instructions if pyinstrument is missing."""
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
    """

    def __init__(self, interval: float = 1e-4) -> None:
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
        The depth counter keeps only the outermost pause toggling it, since
        unbalanced start/stop raises.
        """
        if self._prof is not None and self._depth == 0:
            self._prof.stop()
        self._depth += 1

    def resume(self) -> None:
        self._depth -= 1
        if self._prof is not None and self._depth == 0:
            self._prof.start()

    def get_result(self) -> dict[str, float | str] | None:
        """Summarize the last session, or ``None`` to leave the row unannotated.

        ``None`` when nothing was sampled: a body too fast for the interval would
        otherwise report a ``<no samples>`` hottest frame on every row.
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
    """Return ``("func (file.py:12)", self_seconds)`` for the hottest call site.

    Self time is summed per call site first. pyinstrument records one frame per
    *call*, so a helper invoked N times from a timing loop appears as N sibling
    frames holding 1/N of the time each, while the calling loop's own self time
    accumulates in a single frame. Picking the largest individual frame would
    therefore name the benchmark wrapper rather than the hot callee, and would
    flip between runs depending on how the sampler happened to coalesce
    consecutive samples.

    ``[self]`` frames are pyinstrument's synthetic self-time leaves; their parent's
    ``total_self_time`` already sums them, so counting both would double up.
    """
    totals: dict[tuple[str, str, int | None], float] = {}
    stack = [root]
    while stack:
        f = stack.pop()
        stack.extend(f.children)
        if f.is_synthetic:
            continue
        file_name = Path(f.file_path).name if f.file_path else "?"
        key = (f.function, file_name, f.line_no)
        totals[key] = totals.get(key, 0.0) + f.total_self_time
    if not totals:
        return "<no samples>", 0.0
    (function, file_name, line_no), self_time = max(totals.items(), key=lambda kv: kv[1])
    return f"{function} ({file_name}:{line_no})", self_time


def write_html(sessions: list[Session], path: Path) -> None:
    """Render every captured session into one combined pyinstrument HTML report."""
    from functools import reduce

    from pyinstrument.renderers import HTMLRenderer
    from pyinstrument.session import Session

    if not sessions:
        return
    combined = reduce(Session.combine, sessions)
    path.write_text(HTMLRenderer().render(combined))
