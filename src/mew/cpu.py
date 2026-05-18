"""Optional CPU profiling via pyinstrument."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mew._profile import _MockState

if TYPE_CHECKING:
    from mew._registry import Entry


@dataclass(frozen=True, slots=True)
class CPUProfile:
    profiler: str
    wall_time: float
    sample_count: int
    top_function: str
    top_function_total_self_time: float


def _ensure_pyinstrument() -> None:
    if find_spec("pyinstrument") is None:
        raise SystemExit(
            "pyinstrument is required for CPU profiling. "
            "Install it with: uv add --optional cpu pyinstrument"
        )


def profile(
    entries: list[Entry],
    *,
    output: Path | None = None,
    interval: float = 1e-4,
    inner_iterations: int = 1000,
) -> dict[str, CPUProfile]:
    """Profile each entry under pyinstrument and return per-entry CPUProfiles.

    `inner_iterations` controls how many times the benchmark body runs under
    the sampler — fast benchmarks need many iterations to accumulate samples.
    `interval` is pyinstrument's sampling period in seconds. If *output* is
    given, additionally runs a combined session over all entries and writes
    a pyinstrument HTML report to that path.
    """
    _ensure_pyinstrument()
    profiles = _collect_stats(entries, interval, inner_iterations)
    if output is not None:
        _write_html(entries, output, interval, inner_iterations)
    return profiles


def _collect_stats(
    entries: list[Entry],
    interval: float,
    inner_iterations: int,
) -> dict[str, CPUProfile]:
    import pyinstrument

    profiles: dict[str, CPUProfile] = {}
    for entry in entries:
        prof = pyinstrument.Profiler(interval=interval, async_mode="disabled")
        with prof:
            entry.fn(_MockState(n_iterations=inner_iterations))
        profiles[entry.name] = _summarize(prof.last_session)
    return profiles


def _write_html(
    entries: list[Entry],
    path: Path,
    interval: float,
    inner_iterations: int,
) -> None:
    import pyinstrument

    prof = pyinstrument.Profiler(interval=interval, async_mode="disabled")
    with prof:
        for entry in entries:
            entry.fn(_MockState(n_iterations=inner_iterations))
    path.write_text(prof.output_html())


def _summarize(session: Any) -> CPUProfile:
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


def _hottest_frame(root: Any) -> Any:
    """Find the frame with the largest self_time anywhere in the call tree."""
    best = root
    stack = [root]
    while stack:
        f = stack.pop()
        if f.total_self_time > best.total_self_time:
            best = f
        stack.extend(f.children)
    return best
