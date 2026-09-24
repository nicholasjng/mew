"""User-defined context: dotted keys, snapshots, runner integration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from _helpers import Capture

import mew
from mew.reporter import JSONReporter


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([("commit", "abc123")], {"commit": "abc123"}),
        ([("dataset.size", 1024)], {"dataset": {"size": 1024}}),
        ([("a.b.c.d", 42)], {"a": {"b": {"c": {"d": 42}}}}),
        (
            [("dataset.size", 1024), ("dataset.name", "synthetic")],
            {"dataset": {"size": 1024, "name": "synthetic"}},
        ),
        ([("k", 1), ("k", 2)], {"k": 2}),
        ([("dataset.size", 1024), ("dataset", "scalar")], {"dataset": "scalar"}),
    ],
)
def test_set_context_builds_nested_dicts(calls, expected):
    for key, value in calls:
        mew.set_context(key, value)
    assert mew.get_context() == expected


# None is a stored value like any other, not "absent": traversing through it raises.
@pytest.mark.parametrize("leaf", ["scalar", None])
def test_nesting_under_a_leaf_raises(leaf):
    mew.set_context("dataset", leaf)
    with pytest.raises(ValueError, match="is not a dict"):
        mew.set_context("dataset.size", 1024)


def test_update_context_with_kwargs():
    mew.update_context(commit="abc", env={"python": "3.13"})
    assert mew.get_context() == {"commit": "abc", "env": {"python": "3.13"}}


def test_update_context_dotted_via_splat():
    mew.update_context(**{"dataset.size": 1024, "env.python": "3.13"})
    assert mew.get_context() == {
        "dataset": {"size": 1024},
        "env": {"python": "3.13"},
    }


def test_update_context_with_positional_mapping():
    mew.update_context({"dataset.size": 1024, "commit": "abc"})
    assert mew.get_context() == {
        "dataset": {"size": 1024},
        "commit": "abc",
    }


def test_empty_key_raises():
    with pytest.raises(ValueError, match="non-empty string"):
        mew.set_context("", 1)


def test_empty_segment_raises():
    with pytest.raises(ValueError, match="empty path segment"):
        mew.set_context("dataset..size", 1)


def test_clear_drops_everything():
    mew.set_context("a", 1)
    mew.set_context("b.c", 2)
    mew.clear_context()
    assert mew.get_context() == {}


def test_get_context_returns_a_snapshot():
    mew.set_context("a", {"nested": 1})
    snap = mew.get_context()
    snap["a"]["nested"] = 999
    snap["new"] = "value"
    assert mew.get_context() == {"a": {"nested": 1}}


def test_context_flows_into_reporter():
    mew.set_context("dataset.size", 1024)
    mew.set_context("commit", "abc")

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert cap.context is not None
    # The machine provider is applied by default, so user values sit beside it.
    assert cap.context["context"]["dataset"] == {"size": 1024}
    assert cap.context["context"]["commit"] == "abc"
    assert "cpu_scaling_enabled" in cap.context["context"]


def test_no_custom_key_when_context_is_empty():
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap)
    assert cap.context is not None
    # No injection means no `custom` key shoved into the dict.
    assert "custom" not in cap.context


def test_context_snapshot_is_captured_at_run_start():
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


def test_json_reporter_emits_custom_context(tmp_path: Path):
    mew.set_context("dataset.size", 1024)

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "results.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
    )
    doc = json.loads(out.read_text())
    assert doc["context"]["dataset"] == {"size": 1024}


def test_json_reporter_handles_non_serializable_via_default(tmp_path: Path):
    mew.set_context("path", Path("/tmp/something"))

    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass

    out = tmp_path / "results.json"
    mew.run(
        min_time="1x",
        reporter=JSONReporter(output=out),
    )
    doc = json.loads(out.read_text())
    # Path stringifies via default=str — lossy but doesn't crash.
    expected_val = "/tmp/something" if sys.platform != "win32" else "\\tmp\\something"
    assert doc["context"]["path"] == expected_val
