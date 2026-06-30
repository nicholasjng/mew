"""xctrace ``.trace`` → speedscope (collapsed text or multi-profile JSON).

``xctrace`` won't emit speedscope/pprof directly, but ``xctrace export`` dumps the
Time Profiler samples as XML, which we fold into stacks. From the folded stacks we
write either Brendan-Gregg collapsed text (one profile per file) or speedscope's
own JSON (which packs many profiles into one file behind a dropdown, handy for a
big parametrized family where you want to cycle through cases).

Status: **prototype.** The folding (:func:`fold_samples`) is validated against a
synthetic fixture mirroring the documented export shape, not yet against a real
Xcode-recorded trace. Three things to pin against a live trace before promoting it:

1. **Schema name.** We export ``table[@schema="time-profile"]``; some Xcode
   versions surface the CPU samples under ``time-sample``. See :data:`_XPATH`.
2. **Frame order.** xctrace stores a backtrace leaf-first; both output formats want
   root-first, so we reverse (:data:`_LEAF_FIRST`). Flip if a real trace shows the
   flame graph upside down.
3. **Sample weighting.** We weight every sample 1 (a count), so the JSON ``unit``
   is ``"none"``. Threading xctrace's per-sample interval through would let the
   flame graph read as time.

Port of the algorithm in inferno's ``collapse/xctrace.rs``, trimmed to what we
need: stdlib :mod:`xml.etree.ElementTree` streaming replaces ``quick_xml`` (and
auto-unescapes attribute values), and we drop the Rust-symbol demangling; Xcode
14.3+ already exports symbolicated names.
"""

from __future__ import annotations

import json
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

#: xctrace lists each backtrace leaf (innermost) frame first; both output formats
#: are root-first, so we reverse. Matches inferno's behaviour.
_LEAF_FIRST = True

#: speedscope file-format schema URL stamped into the JSON document.
_SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"

#: A folded stack tally: root-first frame-label tuple → sample count.
Folded = Counter  # alias for readability in signatures (Counter[tuple[str, ...]])


def fold_samples(source: str | Path | IO[bytes]) -> Counter[tuple[str, ...]]:
    """Parse exported xctrace XML into a ``{stack: sample_count}`` tally.

    ``source`` is a path or binary stream of the ``xctrace export --xpath`` output.
    Each ``<row>`` is one sample (weight 1); the returned ``Counter`` maps a
    root-first ``(frame, ...)`` tuple to the number of samples in it. Keying by tuple
    (not a ``"a;b;c"`` string) lets both writers consume it without re-splitting on a
    separator a symbol might contain.

    The export deduplicates with an ``id``/``ref`` scheme: a ``<frame>`` or whole
    ``<backtrace>`` is defined once with an ``id`` and later referenced by ``ref``.
    We resolve both against tables built as we stream, so a ``ref`` always points at
    something already seen.
    """
    # id -> "funcName" (or the raw address when a frame isn't symbolicated).
    frames: dict[str, str] = {}
    # id -> ordered list of frame ids (as stored in the XML, i.e. leaf-first).
    backtraces: dict[str, list[str]] = {}
    folded: Counter[tuple[str, ...]] = Counter()

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
) -> tuple[str, ...]:
    """Resolve one ``<backtrace>`` element to a root-first ``(frame, ...)`` tuple."""
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
    return tuple(labels)


def write_collapsed(folded: Counter[tuple[str, ...]], dest: Path) -> Path:
    """Write a folded tally to ``dest`` as Brendan-Gregg collapsed text. Returns ``dest``.

    One profile per file; sorted for deterministic output. speedscope.app imports
    this format directly.
    """
    with dest.open("w") as fh:
        for stack, count in sorted(folded.items()):
            fh.write(f"{';'.join(stack)} {count}\n")
    return dest


def _sampled_profile(
    name: str,
    folded: Counter[tuple[str, ...]],
    intern: dict[str, int],
    frames: list[dict[str, str]],
) -> dict[str, object]:
    """Build one speedscope ``sampled`` profile, interning frames into the shared table.

    ``intern`` / ``frames`` are shared across every profile in a document, so a frame
    seen in multiple cases is stored once.
    """
    samples: list[list[int]] = []
    weights: list[int] = []
    for stack, count in sorted(folded.items()):  # sorted → deterministic output
        idxs: list[int] = []
        for label in stack:  # root-first, same order as the collapsed text
            i = intern.get(label)
            if i is None:
                i = intern[label] = len(frames)
                frames.append({"name": label})
            idxs.append(i)
        samples.append(idxs)
        weights.append(count)
    return {
        "type": "sampled",
        "name": name,
        "unit": "none",  # weights are sample counts, not time
        "startValue": 0,
        "endValue": sum(weights),
        "samples": samples,
        "weights": weights,
    }


def write_speedscope_json(
    profiles: dict[str, Counter[tuple[str, ...]]],
    dest: Path,
    *,
    name: str = "mew",
) -> Path:
    """Write ``{case_name: folded}`` as a speedscope JSON document. Returns ``dest``.

    One entry → a single-profile file; many entries → a multi-profile file that
    speedscope renders with a profile-selector dropdown (cycle through cases). All
    profiles share one frame table, so common frames are deduplicated across cases.
    """
    frames: list[dict[str, str]] = []
    intern: dict[str, int] = {}
    doc = {
        "$schema": _SPEEDSCOPE_SCHEMA,
        "name": name,
        "exporter": "mew",
        "activeProfileIndex": 0,
        "shared": {"frames": frames},
        "profiles": [_sampled_profile(n, f, intern, frames) for n, f in profiles.items()],
    }
    dest.write_text(json.dumps(doc))
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


def fold_trace(trace: Path, *, exe: str | None = None) -> Counter[tuple[str, ...]]:
    """Export ``trace`` and fold it to a sample tally. Returns the folded ``Counter``.

    The XML is written next to the trace as ``<trace>.xctrace.xml``. Raises
    ``SystemExit`` if the export yielded no samples (benchmark too short, or the wrong
    table schema; see :data:`_XPATH`).
    """
    xml = export_xml(trace, trace.with_suffix(".xctrace.xml"), exe=exe)
    folded = fold_samples(xml)
    if not folded:
        raise SystemExit(
            f"mew: no samples parsed from {trace} (export at {xml}). The benchmark may "
            f"be too short to sample, or this Xcode emits a different table schema than "
            f"{_XPATH!r}; check `xctrace export --input {trace} --toc`."
        )
    return folded


if __name__ == "__main__":  # pragma: no cover - manual smoke test on a real trace
    src = Path(sys.argv[1])
    out = src.with_suffix(".speedscope.json")
    print(write_speedscope_json({src.stem: fold_trace(src)}, out))
