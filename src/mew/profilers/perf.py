"""Linux ``perf`` backend — native-frame profiling via the kernel.

``perf record`` launches the worker and samples it; ``perf script`` then emits a
text format that speedscope.app imports directly (the same portable target as the
py-spy backend). ``--call-graph dwarf`` is used so stacks unwind without
frame-pointer-compiled binaries — heavier, but it works on stock interpreters.

Recording needs a ``perf`` whose version matches the running kernel and usually a
lowered ``kernel.perf_event_paranoid`` — easy on a real Linux host/VM (incl. CI
runners), awkward under Docker Desktop. See docs/guide/profiling-native.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mew.profilers.base import Capabilities, each_case, parse_seconds, worker_argv

if TYPE_CHECKING:
    from mew._registry import Entry


class PerfProfiler:
    name = "perf"
    capabilities = Capabilities(native_frames=True, platforms=frozenset({"linux"}))
    viewer_hint = "speedscope.app (import the perf-script text)"

    def unavailable_reason(self) -> str | None:
        if sys.platform != "linux":
            return "perf is Linux-only"
        if shutil.which("perf") is None:
            return "perf not found on PATH (install your distro's linux-perf/linux-tools package)"
        # Binary present isn't enough: perf_event_paranoid gates recording for
        # processes without CAP_PERFMON/SYS_ADMIN, and locked-down hosts (e.g.
        # GitHub runners, default =4) can't lower it. Probe with a trivial capture
        # so we fail fast with an actionable message instead of a CalledProcessError
        # mid-run. `true` is instant; output is swallowed.
        with tempfile.TemporaryDirectory() as d:
            probe = subprocess.run(
                ["perf", "record", "-o", f"{d}/probe.data", "--", "true"],
                capture_output=True,
                text=True,
            )
        if probe.returncode != 0:
            return (
                "perf can't record here — perf_event_paranoid likely blocks it. "
                "Lower it (`sudo sysctl kernel.perf_event_paranoid=1`) or grant "
                "CAP_PERFMON/CAP_SYS_ADMIN (e.g. a privileged container)."
            )
        return None

    def run(
        self,
        entries: list[Entry],
        *,
        output_dir: Path,
        iterations: int,
        time_limit: str | None = None,
        rate: int = 1000,
        **_: Any,
    ) -> dict[str, Path]:
        perf = shutil.which("perf") or "perf"
        artifacts: dict[str, Path] = {}
        for key, file, name, case, dest in each_case(
            entries, output_dir=output_dir, ext=".perf.txt"
        ):
            data = dest.with_suffix(".data")
            worker = worker_argv(file=file, entry_name=name, case=case, iterations=iterations)
            if time_limit:
                # perf records until the child exits; bound the child with timeout(1).
                worker = ["timeout", str(parse_seconds(time_limit)), *worker]
            subprocess.run(
                [
                    perf,
                    "record",
                    "-g",
                    "--call-graph",
                    "dwarf",
                    "-F",
                    str(rate),
                    "-o",
                    str(data),
                    "--",
                    *worker,
                ],
                check=True,
            )
            # `perf script` text is loadable as-is by speedscope.app.
            with open(dest, "w") as fh:
                subprocess.run([perf, "script", "-i", str(data)], check=True, stdout=fh)
            artifacts[key] = dest
        return artifacts

    def open_artifact(self, path: Path) -> None:
        if shutil.which("speedscope"):
            subprocess.run(["speedscope", str(path)], check=False)
        else:
            print(f"mew: open {path} at https://speedscope.app", file=sys.stderr)
