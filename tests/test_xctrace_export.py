"""Prototype xctrace XML → collapsed-stacks folding.

The folder is exercised against a synthetic fixture mirroring the documented
``xctrace export`` shape (the id/ref dedup, leaf-first frames) — no Xcode needed.
Replace/augment with a real recorded trace once one is available.
"""

from __future__ import annotations

import io
import textwrap
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
    # Leaf-first input reversed to root-first collapsed stacks.
    assert folded == {"main;work": 2, "main;other": 1}


def test_unsymbolicated_frame_falls_back_to_address() -> None:
    xml = (
        b'<trace-query-result><node><row><backtrace id="b">'
        b'<frame id="f" addr="0xdead"/></backtrace></row></node></trace-query-result>'
    )
    assert xe.fold_samples(io.BytesIO(xml)) == {"0xdead": 1}


def test_write_collapsed_is_sorted_and_speedscope_shaped(tmp_path: Path) -> None:
    dest = tmp_path / "out.collapsed.txt"
    xe.write_collapsed(xe.fold_samples(io.BytesIO(_XML)), dest)
    assert dest.read_text() == "main;other 1\nmain;work 2\n"
