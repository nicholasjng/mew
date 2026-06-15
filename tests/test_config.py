"""Loading `[tool.mew]` from pyproject.toml and formatting GB options."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mew.config import format_benchmark_args, load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


def test_load_defaults_when_no_pyproject(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg.benchpaths == ["benchmarks"]
    assert cfg.benchmark_options == {}
    assert cfg.session_tag.tool is None  # unset → auto (jj then git)


def test_load_kebab_case_keys_coerce_to_snake(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew]
        python-files = ["b_*.py"]

        [tool.mew.benchmark-options]
        min-time = "2.0"
        """,
    )
    cfg = load(tmp_path)
    assert cfg.python_files == ["b_*.py"]
    assert cfg.benchmark_options == {"min_time": "2.0"}


def test_load_session_tag_disabled(tmp_path: Path):
    _write(tmp_path, "[tool.mew.session-tag]\nenabled = false\n")
    assert load(tmp_path).session_tag.enabled is False
    assert load(tmp_path).session_tag.tool is None


def test_load_session_tag_tool_and_args(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew.session-tag]
        tool = "hg"
        args = ["id", "-i"]
        """,
    )
    spec = load(tmp_path).session_tag
    assert spec.tool == "hg"
    assert spec.args == ["id", "-i"]


def test_load_rejects_unknown_session_tag_key(tmp_path: Path):
    _write(tmp_path, "[tool.mew.session-tag]\ntoll = 'git'\n")  # typo
    with pytest.raises(ValueError, match="unknown keys in \\[tool.mew.session-tag\\]"):
        load(tmp_path)


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
