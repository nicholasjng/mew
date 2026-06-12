"""py-spy backend — native-frame sampling on Linux/Windows.

``py-spy record --native`` launches the worker, samples Python + native C stacks
from the outside, and writes speedscope JSON (a portable format the browser
viewer and speedscope.app read directly). ``--native`` is unsupported on macOS,
which is why this is not the macOS backend; it also needs ``CAP_SYS_PTRACE`` in
containers (see docker/profile.Dockerfile).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mew.profilers.base import (
    Capabilities,
    each_case,
    open_speedscope_artifact,
    parse_seconds,
    worker_argv,
)

if TYPE_CHECKING:
    from mew._registry import Entry


class PySpyProfiler:
    name = "py-spy"
    capabilities = Capabilities(native_frames=True, platforms=frozenset({"linux", "win32"}))
    viewer_hint = "speedscope.app"

    def unavailable_reason(self) -> str | None:
        if sys.platform == "darwin":
            return "py-spy --native (native frames) is unsupported on macOS"
        if shutil.which("py-spy") is None:
            return "py-spy not found on PATH (install: uv pip install py-spy)"
        return None

    def run(
        self,
        entries: list[Entry],
        *,
        output_dir: Path,
        iterations: int,
        time_limit: str | None = None,
        rate: int = 1000,
        **_: object,
    ) -> dict[str, Path]:
        exe = shutil.which("py-spy") or "py-spy"
        artifacts: dict[str, Path] = {}
        for key, file, name, case, dest in each_case(
            entries, output_dir=output_dir, ext=".speedscope.json"
        ):
            cmd = [
                exe,
                "record",
                "--native",
                "--format",
                "speedscope",
                "--rate",
                str(rate),
                "--output",
                str(dest),
            ]
            if time_limit:
                # Stops sampling after N seconds even if the body runs longer.
                cmd += ["--duration", str(int(parse_seconds(time_limit)))]
            cmd += ["--"]
            cmd += worker_argv(file=file, entry_name=name, case=case, iterations=iterations)
            subprocess.run(cmd, check=True)

            try:
                doc = json.loads(dest.read_text())
            except (OSError, json.JSONDecodeError):
                doc = {}
            if not (doc.get("shared", {}).get("frames") and doc.get("profiles")):
                raise SystemExit(
                    f"mew: py-spy captured no samples for {key!r}. The benchmark likely "
                    f"failed to run (check the traceback above) or was too short to sample "
                    f"(raise --iterations). Artifact: {dest}"
                )
            artifacts[key] = dest

        return artifacts

    def open_artifact(self, path: Path) -> None:
        open_speedscope_artifact(path)
