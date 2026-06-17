"""Optional CPU profiling via pyinstrument."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from mew._profile import _ProfileState, iter_entry_cases

if TYPE_CHECKING:
    from pyinstrument import Profiler
    from pyinstrument.frame import Frame
    from pyinstrument.session import Session

    from mew._registry import Entry


def _sampling_pause(
    prof: Profiler,
) -> Callable[[], AbstractContextManager[None]]:
    """``state.pause()`` factory that suspends ``prof``'s sampling for the block.

    pyinstrument accumulates across ``stop()``/``start()`` and drops the gap. The
    depth counter keeps only the outermost pause toggling it, since unbalanced
    start/stop raises.
    """
    depth = 0

    @contextmanager
    def pause() -> Iterator[None]:
        nonlocal depth
        if depth == 0:
            prof.stop()
        depth += 1
        try:
            yield
        finally:
            depth -= 1
            if depth == 0:
                prof.start()

    return pause


@dataclass(frozen=True, slots=True)
class CPUProfile:
    """Per-case CPU summary captured by pyinstrument.

    Sampled out of the timing loop, so the figures are independent of the
    measured run; ``state.pause()`` regions are excluded.

    Attributes
    ----------
    profiler : str
        Name of the sampling backend, always ``"pyinstrument"``.
    wall_time : float
        Seconds the sampled execution took, including sampler overhead.
    sample_count : int
        Stack samples collected. A low count means the body is too fast for the
        interval; raise ``inner_iterations``.
    top_function : str
        Hottest frame by self time, as ``function (file.py:lineno)``.
        ``"<no samples>"`` when nothing was captured.
    top_function_total_self_time : float
        Seconds of self time attributed to ``top_function``.
    """

    profiler: str
    wall_time: float
    sample_count: int
    top_function: str
    top_function_total_self_time: float


def profile(
    entries: list[Entry],
    *,
    output: Path | None = None,
    interval: float = 1e-4,
    inner_iterations: int = 1000,
) -> dict[str, CPUProfile]:
    """Profile each entry under pyinstrument.

    Parameters
    ----------
    entries : list[Entry]
        Benchmarks to profile.
    output : Path, optional
        If given, additionally writes a combined pyinstrument HTML report to this path.
    interval : float, default 1e-4
        Pyinstrument's sampling period in seconds.
    inner_iterations : int, default 1000
        Times the benchmark body runs under the sampler per entry. Fast
        benchmarks need many iterations to accumulate samples.

    Returns
    -------
    dict[str, CPUProfile]
        Per-case profiles keyed by ``entry.name`` (or ``entry.name/case:<i>`` for
        each variant of a parametrized family).

    Notes
    -----
    ``state.pause()`` regions are excluded: the pause suspends the sampler, as
    ``pause()`` excludes setup from a timed run.
    """
    if find_spec("pyinstrument") is None:
        raise SystemExit(
            "pyinstrument is required for CPU profiling. "
            "Install it with: uv add --optional cpu pyinstrument"
        )
    profiles, sessions = _collect_stats(entries, interval, inner_iterations)
    if output is not None:
        # Render from the sessions already captured for the stats pass; a
        # second execution of the whole suite would double profiling wall time.
        _write_html(sessions, output)
    return profiles


def _collect_stats(
    entries: list[Entry],
    interval: float,
    inner_iterations: int,
) -> tuple[dict[str, CPUProfile], list[Session]]:
    import pyinstrument

    profiles: dict[str, CPUProfile] = {}
    sessions: list[Session] = []
    for entry in entries:
        for key, rng in iter_entry_cases(entry):
            prof = pyinstrument.Profiler(interval=interval, async_mode="disabled")
            # Warn and move on when a body raises: the timed run turns the same
            # error into a skipped row and continues, so one broken benchmark
            # must not abort the whole sampling pass either.
            try:
                with prof:
                    entry.fn(
                        _ProfileState(
                            n_iterations=inner_iterations,
                            range_value=rng,
                            pause=_sampling_pause(prof),
                        )
                    )
            except Exception as e:
                print(
                    f"warning: {key}: body raised during CPU sampling; skipping ({e!r})",
                    file=sys.stderr,
                )
                continue
            session = prof.last_session
            # Set on context-manager exit; always present here.
            assert session is not None
            profiles[key] = _summarize(session)
            sessions.append(session)
    return profiles, sessions


def _write_html(sessions: list[Session], path: Path) -> None:
    from functools import reduce

    from pyinstrument.renderers import HTMLRenderer
    from pyinstrument.session import Session

    if not sessions:
        return
    combined = reduce(Session.combine, sessions)
    path.write_text(HTMLRenderer().render(combined))


def _summarize(session: Session) -> CPUProfile:
    root = session.root_frame()
    if root is None or session.sample_count == 0:
        return CPUProfile(
            profiler="pyinstrument",
            wall_time=session.duration,
            sample_count=session.sample_count,
            top_function="<no samples>",
            top_function_total_self_time=0.0,
        )
    top = _hottest_frame(root)
    file_name = Path(top.file_path).name if top.file_path else "?"
    return CPUProfile(
        profiler="pyinstrument",
        wall_time=session.duration,
        sample_count=session.sample_count,
        top_function=f"{top.function} ({file_name}:{top.line_no})",
        top_function_total_self_time=top.total_self_time,
    )


def _hottest_frame(root: Frame) -> Frame:
    """Find the frame with the largest self_time anywhere in the call tree."""
    best = root
    stack = [root]
    while stack:
        f = stack.pop()
        if f.total_self_time > best.total_self_time:
            best = f
        stack.extend(f.children)
    return best
