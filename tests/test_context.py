"""User-defined context: dotted keys, snapshots, runner integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import Capture

import mew
from mew.reporter import JSONReporter


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([("commit", "abc123")], {"commit": "abc123"}),
        (
            [("dataset.size", 1024), ("dataset.name", "synthetic")],
            {"dataset": {"size": 1024, "name": "synthetic"}},
        ),
        # The last write wins in either order: a value replaces what was nested
        # below its key, and a dotted key replaces a non-dict value with a dict.
        ([("dataset.size", 1024), ("dataset", "scalar")], {"dataset": "scalar"}),
        ([("dataset", "scalar"), ("dataset.size", 1024)], {"dataset": {"size": 1024}}),
        ([("dataset", None), ("dataset.size", 1024)], {"dataset": {"size": 1024}}),
        ([("a.x", 0), ("a.b", 1), ("a.b.c", 2)], {"a": {"x": 0, "b": {"c": 2}}}),
    ],
)
def test_set_context_nests_dotted_keys(calls, expected):
    """Dotted keys nest into dicts, and the last write to a key or to its parent wins."""
    for key, value in calls:
        mew.set_context(key, value)
    assert mew.get_context() == expected


def test_set_context_copies_values():
    """`set_context` stores deep copies: dotted writes and the caller's later changes stay apart."""
    dataset, shape = {"size": 1024, "source": {"name": "uniform"}}, [2, 3]
    mew.set_context("dataset", dataset)
    mew.set_context("shape", shape)
    mew.set_context("dataset.size", 1)
    mew.set_context("dataset.source.name", "zipf")
    shape.append(4)
    assert dataset == {"size": 1024, "source": {"name": "uniform"}}
    assert mew.get_context()["shape"] == [2, 3]


def test_update_context_applies_mappings_then_kwargs():
    """`update_context` nests dotted keys from mappings and keyword arguments; keywords win."""
    # Dotted keys nest in both forms; keyword arguments override the mappings.
    mew.update_context(
        {"dataset.size": 1024, "commit": "abc"},
        commit="def",
        **{"env.python": "3.13"},
    )
    assert mew.get_context() == {
        "dataset": {"size": 1024},
        "commit": "def",
        "env": {"python": "3.13"},
    }


@pytest.mark.parametrize(
    ("key", "message"), [("", "non-empty string"), ("dataset..size", "empty path segment")]
)
def test_malformed_key_raises(key, message):
    """An empty key or an empty dotted path segment raises `ValueError`."""
    with pytest.raises(ValueError, match=message):
        mew.set_context(key, 1)


def test_get_context_returns_a_snapshot():
    """`get_context` returns a deep copy; changing it leaves the stored context as it was."""
    mew.set_context("a", {"nested": 1})
    snap = mew.get_context()
    snap["a"]["nested"] = 999
    snap["new"] = "value"
    assert mew.get_context() == {"a": {"nested": 1}}


def test_context_snapshot_is_captured_at_run_start():
    """Reporters get the context as it was when `mew.run` started, not as a benchmark changed it."""
    mew.set_context("snapshot_phase", "before")

    @mew.benchmark
    def bench_x(state):
        # Mutating during the run shouldn't affect the snapshot the reporter sees.
        mew.set_context("snapshot_phase", "during")
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert cap.context is not None
    assert cap.context["context"]["snapshot_phase"] == "before"


def test_json_reporter_writes_what_json_cannot_as_str(tmp_path: Path):
    """`JSONReporter` writes a context value JSON cannot encode, such as a `Path`, as its `str`."""
    mew.set_context("path", Path("data.csv"))

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "results.json"
    mew.run(min_time="1x", reporter=JSONReporter(output=out))
    # Lossy, but the run doesn't crash on a Path or a datetime.
    assert json.loads(out.read_text())["context"]["path"] == "data.csv"
