"""Prototype: xctrace ``.trace`` → collapsed stacks (speedscope-loadable).

``xctrace`` won't emit speedscope/pprof directly, but ``xctrace export`` dumps the
Time Profiler samples as XML, which we fold into Brendan-Gregg collapsed stacks
(``frame;frame;frame count`` per line) — the same portable text the perf backend
hands speedscope.app today.

Status: **prototype.** The folding (:func:`fold_samples`) is validated against a
synthetic fixture mirroring the documented export shape, not yet against a real
Xcode-recorded trace. Two things to pin against a live trace before promoting it:

1. **Schema name.** We export ``table[@schema="time-profile"]``; some Xcode
   versions surface the CPU samples under ``time-sample``. See :data:`_XPATH`.
2. **Frame order.** xctrace stores a backtrace leaf-first; collapsed wants
   root-first, so we reverse (:data:`_LEAF_FIRST`). Flip if a real trace shows the
   flame graph upside down.

Port of the algorithm in inferno's ``collapse/xctrace.rs``, trimmed to what we
need: stdlib :mod:`xml.etree.ElementTree` streaming replaces ``quick_xml`` (and
auto-unescapes attribute values), and we drop the Rust-symbol demangling — Xcode
14.3+ already exports symbolicated names.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import IO

#: xpath into the export's table-of-contents selecting the Time Profiler samples.
#: Discover the available tables for a given trace with ``xctrace export --toc``.
_XPATH = '/trace-toc[1]/run[1]/data[1]/table[@schema="time-profile"]'

#: xctrace lists each backtrace leaf (innermost) frame first; collapsed/folded
#: output is root-first, so we reverse. Matches inferno's behaviour.
_LEAF_FIRST = True


def fold_samples(source: str | Path | IO[bytes]) -> Counter[str]:
    """Parse exported xctrace XML into a ``{stack: sample_count}`` tally.

    ``source`` is a path or binary stream of the ``xctrace export --xpath`` output.
    Each ``<row>`` is one sample (weight 1); the returned ``Counter`` maps a folded
    ``"root;...;leaf"`` stack to the number of samples that landed in it.

    The export deduplicates with an ``id``/``ref`` scheme — a ``<frame>`` or whole
    ``<backtrace>`` is defined once with an ``id`` and later referenced by ``ref``.
    We resolve both against tables built as we stream, so a ``ref`` always points at
    something already seen.
    """
    # id -> "funcName" (or the raw address when a frame isn't symbolicated).
    frames: dict[str, str] = {}
    # id -> ordered list of frame ids (as stored in the XML, i.e. leaf-first).
    backtraces: dict[str, list[str]] = {}
    folded: Counter[str] = Counter()

    # iterparse fires `end` for a <frame> before its enclosing <backtrace>, and for
    # the <backtrace> before its <row>; so at a backtrace's `end` its frame children
    # are fully populated. We resolve there and clear the row afterwards to bound memory.
    context = ET.iterparse(source, events=("end",))
    for _event, elem in context:
        tag = elem.tag
        if tag == "backtrace":
            stack = _resolve_backtrace(elem, frames, backtraces)
            if stack:  # skip rows whose backtrace didn't resolve to any frame
                folded[stack] += 1
        elif tag == "row":
            elem.clear()  # frame/backtrace defs are in our dicts now; drop the XML
    return folded


def _resolve_backtrace(
    elem: ET.Element,
    frames: dict[str, str],
    backtraces: dict[str, list[str]],
) -> str:
    """Resolve one ``<backtrace>`` element to a folded ``root;...;leaf`` string."""
    ref = elem.get("ref")
    if ref is not None:
        frame_ids = backtraces.get(ref, [])
    else:
        frame_ids = []
        for frame in elem.findall("frame"):
            fref = frame.get("ref")
            if fref is not None:
                fid = fref
            else:
                fid = frame.get("id", "")
                # Prefer the symbol; fall back to the address for unsymbolicated frames.
                frames[fid] = frame.get("name") or frame.get("addr") or "<unknown>"
            frame_ids.append(fid)
        bid = elem.get("id")
        if bid is not None:
            backtraces[bid] = frame_ids

    labels = [frames.get(fid, "<unknown>") for fid in frame_ids]
    if _LEAF_FIRST:
        labels.reverse()
    return ";".join(labels)


def write_collapsed(folded: Counter[str], dest: Path) -> Path:
    """Write a ``{stack: count}`` tally to ``dest`` as collapsed text. Returns ``dest``.

    Sorted for deterministic output; speedscope.app imports this format directly.
    """
    with dest.open("w") as fh:
        for stack, count in sorted(folded.items()):
            fh.write(f"{stack} {count}\n")
    return dest


def export_xml(trace: Path, dest: Path, *, exe: str | None = None) -> Path:
    """Run ``xctrace export`` on ``trace``, writing the Time Profiler XML to ``dest``.

    Returns ``dest``. Raises ``subprocess.CalledProcessError`` if xctrace fails.
    """
    exe = exe or shutil.which("xctrace") or "/usr/bin/xctrace"
    with dest.open("w") as fh:
        subprocess.run(
            [exe, "export", "--input", str(trace), "--xpath", _XPATH],
            check=True,
            stdout=fh,
        )
    return dest


def trace_to_collapsed(trace: Path, dest: Path, *, exe: str | None = None) -> Path:
    """End-to-end: ``.trace`` bundle → collapsed ``.txt`` at ``dest``. Returns ``dest``.

    Raises ``SystemExit`` if the export yielded no samples (benchmark too short, or
    the wrong table schema — see :data:`_XPATH`).
    """
    xml = export_xml(trace, dest.with_suffix(".xctrace.xml"), exe=exe)
    folded = fold_samples(xml)
    if not folded:
        raise SystemExit(
            f"mew: no samples parsed from {trace} (export at {xml}). The benchmark may "
            f"be too short to sample, or this Xcode emits a different table schema than "
            f"{_XPATH!r} — check `xctrace export --input {trace} --toc`."
        )
    return write_collapsed(folded, dest)


if __name__ == "__main__":  # pragma: no cover - manual smoke test on a real trace
    src = Path(sys.argv[1])
    out = src.with_suffix(".collapsed.txt")
    print(trace_to_collapsed(src, out))
