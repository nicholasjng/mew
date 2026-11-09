"""A run's context: what `mew.run(context=...)` records, and how its values are encoded."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from _helpers import Capture

import mew
from mew.reporter import JSONReporter


def _bench() -> None:
    @mew.benchmark
    def bench_x(state):
        for _ in state:
            pass


def test_run_records_its_context_beside_the_machines():
    """`context=` lands in every row's `context`, beside mew's version and the machine's
    details; its keys win over mew's own."""
    _bench()
    cap = Capture()
    context = {"dataset": {"name": "uniform", "size": 1024}, "cpu_model": "named by the suite"}
    mew.run(min_time="1x", reporter=cap, context=context)
    assert cap.context is not None
    recorded = cap.context["context"]
    assert recorded["dataset"] == {"name": "uniform", "size": 1024}
    assert recorded["cpu_model"] == "named by the suite"
    assert recorded["mew"] == mew.__version__
    assert "python" in recorded


def test_context_is_captured_at_run_start():
    """Reporters get the context as it was when `mew.run` started, not as a benchmark changed
    the dict it was given."""
    context = {"phase": "before"}

    @mew.benchmark
    def bench_x(state):
        context["phase"] = "during"
        for _ in state:
            pass

    cap = Capture()
    mew.run(min_time="1x", reporter=cap, context=context)
    assert cap.context is not None
    assert cap.context["context"]["phase"] == "before"


class _Model:
    """Stands in for an object whose `str()` would be large or meaningless."""

    def __str__(self) -> str:
        return "x" * 100_000


def test_context_values_json_cannot_hold_are_named_by_type(tmp_path: Path):
    """A value JSON can't represent is stored as its type's qualified name, never its `str()`;
    a path as its string; tuples as lists, number keys as strings, and NaN as JSON's `NaN`."""
    _bench()
    out = tmp_path / "results.json"
    context = {
        "model": _Model(),
        "data": Path("data") / "train.csv",
        "shape": (2, 3),
        "by_size": {1: "small"},
        "loss": math.nan,
    }
    mew.run(min_time="1x", reporter=JSONReporter(output=out), context=context)
    (row,) = json.loads(out.read_text())["benchmarks"]
    recorded = row["context"]
    assert recorded["model"] == f"{__name__}._Model"
    assert recorded["data"] == str(Path("data") / "train.csv")
    assert recorded["shape"] == [2, 3]
    assert recorded["by_size"] == {"1": "small"}
    assert math.isnan(recorded["loss"])


@pytest.mark.parametrize("context", [["not", "a", "mapping"], {1: "a non-string key"}])
def test_run_rejects_a_context_that_is_not_a_mapping_with_string_keys(context):
    """A context must be a mapping with string keys: row context is a JSON object."""
    _bench()
    with pytest.raises(TypeError, match="context must be a mapping with string keys"):
        mew.run(min_time="1x", reporter=Capture(), context=context)
