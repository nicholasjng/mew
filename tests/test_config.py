"""Loading `[tool.mew]` from pyproject.toml."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mew.config import load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


def test_load_defaults_when_no_pyproject(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg.benchpaths == ["benchmarks"]
    assert cfg.session_tag.tool is None  # unset → auto (jj then git)
    assert cfg.statistic is None


def test_load_bare_string_benchpaths_is_one_path(tmp_path: Path):
    # A bare TOML string must be one path, not list("perf") == ["p","e","r","f"].
    _write(
        tmp_path,
        """
        [tool.mew]
        benchpaths = "perf"
        python-files = "bench_*.py"
        """,
    )
    cfg = load(tmp_path)
    assert cfg.benchpaths == ["perf"]
    assert cfg.python_files == ["bench_*.py"]


def test_load_rejects_non_string_benchpaths(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew]
        benchpaths = 42
        """,
    )
    with pytest.raises(ValueError, match="benchpaths"):
        load(tmp_path)


def test_load_statistic_reference(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew]
        statistic = "scipy.stats:gmean"
        """,
    )
    assert load(tmp_path).statistic == "scipy.stats:gmean"


def test_load_rejects_non_string_statistic(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew]
        statistic = 95
        """,
    )
    with pytest.raises(ValueError, match="statistic must be a 'module.path:attr' string"):
        load(tmp_path)


def test_load_kebab_case_keys_coerce_to_snake(tmp_path: Path):
    _write(
        tmp_path,
        """
        [tool.mew]
        python-files = ["b_*.py"]
        """,
    )
    cfg = load(tmp_path)
    assert cfg.python_files == ["b_*.py"]


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
