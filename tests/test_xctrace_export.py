"""Prototype xctrace XML → speedscope multi-profile JSON.

The folder/emitters are exercised against a synthetic fixture mirroring the
documented ``xctrace export`` shape (the id/ref dedup, leaf-first frames) — no Xcode
needed. Replace/augment with a real recorded trace once one is available.
"""

from __future__ import annotations

import io
import json
import textwrap
from collections import Counter
from pathlib import Path

from mew.profilers import _xctrace_export as xe

# Three samples. Backtrace b1 is defined once (frames f1=main, f2=work, leaf-first),
# row 2 reuses a frame by ref (f1) under a new backtrace, and row 3 reuses the whole
# backtrace b1 by ref — exercising both dedup levels.
_XML = textwrap.dedent(
    """\
    <trace-query-result>
      <node>
        <row>
          <sample-time id="1" fmt="00:01.000">1000</sample-time>
          <backtrace id="b1">
            <frame id="f2" name="work" addr="0x200"/>
            <frame id="f1" name="main" addr="0x100"/>
          </backtrace>
        </row>
        <row>
          <sample-time id="2" fmt="00:02.000">2000</sample-time>
          <backtrace id="b2">
            <frame id="f3" name="other" addr="0x300"/>
            <frame ref="f1"/>
          </backtrace>
        </row>
        <row>
          <sample-time ref="1"/>
          <backtrace ref="b1"/>
        </row>
      </node>
    </trace-query-result>
    """
).encode()


def test_fold_resolves_refs_and_orders_root_first() -> None:
    folded = xe.fold_samples(io.BytesIO(_XML))
    # Leaf-first input reversed to root-first stacks, keyed by tuple (not "a;b;c").
    assert folded == {("main", "work"): 2, ("main", "other"): 1}


def test_unsymbolicated_frame_falls_back_to_address() -> None:
    xml = (
        b'<trace-query-result><node><row><backtrace id="b">'
        b'<frame id="f" addr="0xdead"/></backtrace></row></node></trace-query-result>'
    )
    assert xe.fold_samples(io.BytesIO(xml)) == {("0xdead",): 1}


def test_speedscope_json_single_profile_shape(tmp_path: Path) -> None:
    folded = xe.fold_samples(io.BytesIO(_XML))
    dest = tmp_path / "one.speedscope.json"
    xe.write_speedscope_json({"bench[n=1]": folded}, dest)
    doc = json.loads(dest.read_text())

    assert doc["$schema"] == xe._SPEEDSCOPE_SCHEMA
    (profile,) = doc["profiles"]
    assert profile["type"] == "sampled"
    assert profile["name"] == "bench[n=1]"
    assert profile["endValue"] == 3  # total samples (2 + 1)

    # Samples carry frame *indices* into shared.frames; resolve them back to names.
    frames = [f["name"] for f in doc["shared"]["frames"]]
    resolved = {
        tuple(frames[i] for i in stack): w
        for stack, w in zip(profile["samples"], profile["weights"], strict=True)
    }
    assert resolved == {("main", "other"): 1, ("main", "work"): 2}


def test_speedscope_json_combines_cases_and_shares_frames(tmp_path: Path) -> None:
    a: Counter[tuple[str, ...]] = Counter({("main", "work"): 3})
    b: Counter[tuple[str, ...]] = Counter({("main", "parse"): 2})
    dest = tmp_path / "mew.speedscope.json"
    xe.write_speedscope_json({"bench[n=1]": a, "bench[n=2]": b}, dest)
    doc = json.loads(dest.read_text())

    # One document, one profile per case (the dropdown), and `main` interned once.
    assert [p["name"] for p in doc["profiles"]] == ["bench[n=1]", "bench[n=2]"]
    names = [f["name"] for f in doc["shared"]["frames"]]
    assert sorted(names) == ["main", "parse", "work"]  # main shared, not duplicated
    assert doc["activeProfileIndex"] == 0
