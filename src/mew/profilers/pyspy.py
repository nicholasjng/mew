"""py-spy backend — native-frame sampling on Linux/Windows.

``py-spy record --native`` launches the worker, samples Python + native C stacks
from the outside, and writes speedscope JSON (a portable format the browser
viewer and speedscope.app read directly). ``--native`` is unsupported on macOS,
which is why this is not the macOS backend; it also needs ``CAP_SYS_PTRACE`` in
containers (see docker/profile.Dockerfile).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mew.profilers.base import Capabilities, each_case, parse_seconds, worker_argv

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
            artifacts[key] = dest
        return artifacts

    def open_artifact(self, path: Path) -> None:
        # `npx speedscope <file>` opens the browser viewer; otherwise point at the web app.
        if shutil.which("speedscope"):
            subprocess.run(["speedscope", str(path)], check=False)
        else:
            print(f"mew: open {path} at https://speedscope.app", file=sys.stderr)
