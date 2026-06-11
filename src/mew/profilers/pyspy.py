"""py-spy backend — native-frame sampling on Linux/Windows.

Stub: availability probing is wired up so ``auto`` selection and error messages
work; the recording path is not implemented yet. When built out, it will wrap
:mod:`mew._subprocess_worker` with ``py-spy record --native -o <out> --format
speedscope -- <worker>`` (speedscope JSON so the planned browser viewer can
render it). ``--native`` is unsupported on macOS, which is why this is not the
macOS backend.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mew.profilers.base import Capabilities

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
            return "py-spy not found on PATH (install: uv add --optional profile py-spy)"
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
        raise SystemExit("mew: the py-spy backend is not implemented yet.")

    def open_artifact(self, path: Path) -> None:
        # Future: open speedscope.app in the browser with the JSON.
        pass
