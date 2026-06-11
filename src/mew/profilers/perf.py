"""Linux ``perf`` backend — native-frame profiling via the kernel.

Stub: availability probing is wired up so ``auto`` selection and error messages
work; the recording path is not implemented yet. When built out, it will wrap
:mod:`mew._subprocess_worker` with ``perf record -g -o <out>.data -- <worker>``,
then ``perf script`` → folded stacks for a flamegraph / speedscope export.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mew.profilers.base import Capabilities

if TYPE_CHECKING:
    from mew._registry import Entry


class PerfProfiler:
    name = "perf"
    capabilities = Capabilities(native_frames=True, platforms=frozenset({"linux"}))
    viewer_hint = "perf report / a flamegraph viewer"

    def unavailable_reason(self) -> str | None:
        if sys.platform != "linux":
            return "perf is Linux-only"
        if shutil.which("perf") is None:
            return "perf not found on PATH (install your distro's linux-perf/linux-tools package)"
        # Note: recording may still require lowering /proc/sys/kernel/perf_event_paranoid.
        return None

    def run(
        self,
        entries: list[Entry],
        *,
        output_dir: Path,
        iterations: int,
        time_limit: str | None = None,
        **_: object,
    ) -> dict[str, Path]:
        raise SystemExit("mew: the perf backend is not implemented yet.")

    def open_artifact(self, path: Path) -> None:
        pass
