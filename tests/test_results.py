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


def test_json_rows_inherit_the_document_context_unless_they_carry_their_own(tmp_path: Path):
    path = tmp_path / "results.json"
    header = {"session": {"id": "header"}, "context": {"engine": "header"}}
    rows = [
        row("inherited", 1),
        row("own", 2, session_id="own", custom={"engine": "own"}),
        row("empty", 3, session={}, context={}),
    ]
    path.write_text(json.dumps({"context": header, "benchmarks": rows}))

    inherited, own, empty = read_results(path)
    assert inherited["session"] == header["session"]
    assert inherited["context"] == header["context"]
    assert own["session"] == {"id": "own"}
    assert own["context"] == {"engine": "own"}
    assert empty["session"] == {}
    assert empty["context"] == {}
    assert {s.id for s in session_summaries(path)} == {None, "header", "own"}
