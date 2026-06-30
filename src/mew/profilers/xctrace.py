"""xctrace (Instruments) backend: macOS native-frame profiling.

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
    from collections import Counter

    from mew._registry import Entry

#: Default Instruments template. Pass a template name (as shown by
#: ``xctrace list templates``) or a path to a ``.tracetemplate``.
DEFAULT_TEMPLATE = "Time Profiler"
#: Name of the combined trace bundle written when ``separate`` is false.
COMBINED_NAME = "mew.trace"
#: Output formats this backend accepts. ``auto`` (the platform-agnostic default,
#: mirroring ``--profiler auto``) and its tool-named alias ``xctrace`` both yield
#: the native Instruments ``.trace`` bundle; ``speedscope`` folds it to a
#: speedscope JSON document (one profile per case). ``pprof`` is the planned
#: sibling (see ROADMAP); the same format axis that gates perf.
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

        # `speedscope` exports each `.trace` via an xpath that reads a single run, so
        # every case must record into its own bundle (a combined bundle interleaves
        # runs under one toc). `separate` then controls only the *output* layout: one
        # combined JSON (dropdown over cases) vs. one file per case.
        speedscope = format == "speedscope"
        record_per_case = separate or speedscope

        combined = output_dir / COMBINED_NAME
        # xctrace refuses to overwrite a bundle (and would otherwise append this
        # invocation's runs to a stale one), so start each combined run fresh.
        if not record_per_case and combined.exists():
            shutil.rmtree(combined)

        artifacts: dict[str, Path] = {}
        # case_name -> folded sample tally, accumulated for the speedscope path.
        folded: dict[str, Counter[tuple[str, ...]]] = {}
        combined_started = False
        for entry in entries:
            if entry.file is None:
                print(f"mew: skipping {entry.name}: no source file to launch", file=sys.stderr)
                continue
            for key, case in iter_entry_cases(entry):
                if record_per_case:
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

                if speedscope:
                    from mew.profilers._xctrace_export import fold_trace

                    folded[key] = fold_trace(dest, exe=exe)
                else:
                    artifacts[key] = dest

        if speedscope:
            artifacts = self._write_speedscope(folded, output_dir, separate=separate)
        return artifacts

    @staticmethod
    def _write_speedscope(
        folded: dict[str, Counter[tuple[str, ...]]],
        output_dir: Path,
        *,
        separate: bool,
    ) -> dict[str, Path]:
        """Write folded tallies as speedscope JSON: one file per case, or one combined.

        Combined (the default) packs every case into one document behind a profile
        dropdown; ``separate`` writes a single-profile file per case instead.
        """
        from mew.profilers._xctrace_export import write_speedscope_json

        if separate:
            return {
                key: write_speedscope_json(
                    {key: tally}, output_dir / f"{slug(key)}.speedscope.json"
                )
                for key, tally in folded.items()
            }
        # One document, N profiles; every key resolves to it (CLI dedups on open).
        dest = write_speedscope_json(folded, output_dir / "mew.speedscope.json")
        return dict.fromkeys(folded, dest)

    def open_artifact(self, path: Path) -> None:
        # speedscope JSON / collapsed text → speedscope; a `.trace` → Instruments.
        if path.suffix in (".json", ".txt"):
            open_speedscope_artifact(path)
        else:
            subprocess.run(["open", "-a", "Instruments", str(path)], check=False)
