"""xctrace (Instruments) backend — macOS native-frame profiling.

For each benchmark case we shell out to ``xctrace record``, which ``--launch``es
:mod:`mew._subprocess_worker` to drive that one case while xctrace samples it.
The deliverable is a ``.trace`` bundle you open in Instruments.app.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mew._profile import iter_entry_cases
from mew.profilers.base import Capabilities, open_speedscope_artifact, slug, worker_argv

if TYPE_CHECKING:
    from mew._registry import Entry

#: Default Instruments template. Pass a template name (as shown by
#: ``xctrace list templates``) or a path to a ``.tracetemplate``.
DEFAULT_TEMPLATE = "Time Profiler"
#: Name of the combined trace bundle written when ``separate`` is false.
COMBINED_NAME = "mew.trace"
#: Output formats this backend accepts. ``auto`` (the platform-agnostic default,
#: mirroring ``--profiler auto``) and its tool-named alias ``xctrace`` both yield
#: the native Instruments ``.trace`` bundle; ``speedscope`` folds it to
#: speedscope-loadable collapsed stacks. ``pprof`` is the planned sibling (see
#: ROADMAP) — the same format axis that gates perf.
FORMATS = ("auto", "xctrace", "speedscope")


class XctraceProfiler:
    """Records Instruments traces via ``xctrace`` (macOS only)."""

    name = "xctrace"
    capabilities = Capabilities(native_frames=True, platforms=frozenset({"darwin"}))
    viewer_hint = "Instruments.app"

    def unavailable_reason(self) -> str | None:
        if sys.platform != "darwin":
            return "xctrace is macOS-only"
        exe = shutil.which("xctrace") or "/usr/bin/xctrace"
        probe = subprocess.run([exe, "version"], capture_output=True, text=True)
        if probe.returncode != 0:
            # The /usr/bin shim needs full Xcode; Command Line Tools alone won't do.
            return (
                "xctrace needs the full Xcode (not just Command Line Tools). "
                "Install Xcode, then: "
                "sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
            )
        return None

    def run(
        self,
        entries: list[Entry],
        *,
        output_dir: Path,
        iterations: int,
        time_limit: str | None = None,
        template: str = DEFAULT_TEMPLATE,
        separate: bool = False,
        format: str = "auto",
        **_: object,
    ) -> dict[str, Path]:
        exe = shutil.which("xctrace") or "/usr/bin/xctrace"
        output_dir.mkdir(parents=True, exist_ok=True)

        # `speedscope` folds one `.trace` to one collapsed file, and the export
        # xpath reads a single run — so force per-case bundles, where each maps 1:1
        # to a converted file (a combined bundle interleaves runs under one toc).
        if format == "speedscope":
            separate = True

        combined = output_dir / COMBINED_NAME
        # xctrace refuses to overwrite a bundle (and would otherwise append this
        # invocation's runs to a stale one), so start each combined run fresh.
        if not separate and combined.exists():
            shutil.rmtree(combined)

        artifacts: dict[str, Path] = {}
        combined_started = False
        for entry in entries:
            if entry.file is None:
                print(f"mew: skipping {entry.name}: no source file to launch", file=sys.stderr)
                continue
            for key, case in iter_entry_cases(entry):
                if separate:
                    dest = output_dir / f"{slug(key)}.trace"
                    if dest.exists():
                        shutil.rmtree(dest)
                    append = False
                else:
                    dest = combined
                    append = combined_started
                    combined_started = True

                cmd = [exe, "record", "--template", template, "--output", str(dest)]
                if append:
                    # Add this case as another run inside the existing bundle.
                    cmd.append("--append-run")
                if time_limit:
                    cmd += ["--time-limit", time_limit]

                cmd += ["--target-stdout", "-", "--launch", "--"]
                cmd += worker_argv(
                    file=entry.file, entry_name=entry.name, case=case, iterations=iterations
                )
                subprocess.run(cmd, check=True)

                if format == "speedscope":
                    # Fold the bundle to speedscope-loadable collapsed text and hand
                    # *that* back as the artifact, not the `.trace`.
                    from mew.profilers._xctrace_export import trace_to_collapsed

                    artifacts[key] = trace_to_collapsed(
                        dest, output_dir / f"{slug(key)}.collapsed.txt", exe=exe
                    )
                else:
                    artifacts[key] = dest
        return artifacts

    def open_artifact(self, path: Path) -> None:
        # Collapsed text goes to speedscope; a `.trace` bundle opens in Instruments.
        if path.suffix == ".txt":
            open_speedscope_artifact(path)
        else:
            subprocess.run(["open", "-a", "Instruments", str(path)], check=False)
