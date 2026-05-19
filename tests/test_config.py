"""Loading `[tool.mew]` from pyproject.toml and formatting GB options."""

from __future__ import annotations

import textwrap
from pathlib import Path

from mew.config import format_benchmark_args, load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


def test_load_defaults_when_no_pyproject(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg.benchpaths == ["benchmarks"]
    assert cfg.benchmark_options == {}


def test_load_benchmark_options_table(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew.benchmark_options]
        min_time = "2.0"
        repetitions = 5
        report_aggregates_only = true
        """,
    )
    cfg = load(tmp_path)
    assert cfg.benchmark_options == {
        "min_time": "2.0",
        "repetitions": 5,
        "report_aggregates_only": True,
    }


def test_format_benchmark_args_handles_strings_numbers_and_bools():
    args = format_benchmark_args(
        {"min_time": "2.0", "repetitions": 5, "report_aggregates_only": True, "color": False}
    )
    assert args == [
        "--benchmark_min_time=2.0",
        "--benchmark_repetitions=5",
        "--benchmark_report_aggregates_only",
    ]


def test_format_benchmark_args_empty():
    assert format_benchmark_args({}) == []
