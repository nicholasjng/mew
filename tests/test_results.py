"""Result decoding resolves metadata before session selection."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from _helpers import row

from mew.compare import Sample, SessionData, read_results, read_sessions


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".jsonl.gz"])
def test_row_metadata_overrides_header_and_missing_fields_inherit(tmp_path: Path, suffix: str):
    path = tmp_path / ("results" + suffix)
    header = {"session": {"id": "header"}, "context": {"engine": "header"}}
    rows = [
        row("inherited", 1),
        row("own", 2, session_id="own", custom={"engine": "own"}),
        row("empty", 3, session={}, context={}),
    ]
    if suffix == ".json":
        text = json.dumps({"context": header, "benchmarks": rows})
    else:
        text = "\n".join(json.dumps(obj) for obj in [{"context": header}, *rows])
    if suffix.endswith(".gz"):
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)

    inherited, own, empty = read_results(path)
    assert inherited["session"] == header["session"]
    assert inherited["context"] == header["context"]
    assert own["session"] == {"id": "own"}
    assert own["context"] == {"engine": "own"}
    assert empty["session"] == {}
    assert empty["context"] == {}
    sessions = read_sessions(path)
    assert {s.session_id for s in sessions} == {None, "header", "own"}
    assert all(isinstance(s, SessionData) for s in sessions)
    assert all(isinstance(sample, Sample) for s in sessions for sample in s.samples.values())


@pytest.mark.parametrize("compressed", [False, True])
def test_jsonl_headers_only_apply_to_their_own_segment(tmp_path: Path, compressed: bool):
    path = tmp_path / ("sessions.jsonl.gz" if compressed else "sessions.jsonl")
    objects = [
        row("before_headers", 1),
        {"context": {"session": {"id": "first"}}},
        row("first", 2),
        {"context": {"session": {"id": "second"}, "context": {"engine": "second"}}},
        row("second", 3),
    ]
    text = "\n".join(json.dumps(obj) for obj in objects)
    if compressed:
        with gzip.open(path, "wt") as fh:
            fh.write(text)
    else:
        path.write_text(text)
    rows = read_results(path)
    assert "session" not in rows[0] and "context" not in rows[0]
    assert rows[1]["session"] == {"id": "first"}
    assert "context" not in rows[1]
    assert rows[2]["context"] == {"engine": "second"}
    sessions = {s.session_id: s for s in read_sessions(path)}
    assert set(sessions) == {None, "first", "second"}
    assert sessions["first"].provenance == {}
    assert sessions["second"].provenance == {"engine": "second"}
