"""Result decoding resolves metadata before session selection."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from _helpers import row

from mew.compare import read_results, session_summaries


@pytest.mark.parametrize("compressed", [False, True])
def test_jsonl_rows_are_self_contained(tmp_path: Path, compressed: bool):
    path = tmp_path / ("sessions.jsonl.gz" if compressed else "sessions.jsonl")
    rows = [
        row("first", 2, session_id="first"),
        row("second", 3, session_id="second", custom={"engine": "second"}),
    ]
    text = "\n".join(json.dumps(r) for r in rows) + "\n"
    if compressed:
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    assert {s.id for s in session_summaries(path)} == {"first", "second"}
    by_id = {r["session"]["id"]: r for r in read_results(path)}
    assert "context" not in by_id["first"]
    assert by_id["second"]["context"] == {"engine": "second"}
