"""Loading `[tool.mew]` from pyproject.toml."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mew.config import Config, load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


def test_load_defaults_when_no_pyproject(tmp_path: Path):
    cfg = load(tmp_path)
    assert cfg.benchpaths == ["benchmarks"]
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
    with pytest.raises(ValueError, match="statistic must be a string"):
        load(tmp_path)


def test_load_setup_path(tmp_path: Path):
    _write(tmp_path, '[tool.mew]\nsetup = "benchmarks/conf.py"\n')
    assert load(tmp_path).setup == "benchmarks/conf.py"


def test_load_empty_table_uses_dataclass_defaults(tmp_path: Path):
    # Defaults live only on the dataclass; the loader just records the root.
    _write(tmp_path, "[tool.mew]\n")
    assert load(tmp_path) == Config(project_root=tmp_path.resolve())


def test_load_rejects_non_string_setup(tmp_path: Path):
    _write(tmp_path, "[tool.mew]\nsetup = 3\n")
    with pytest.raises(ValueError, match="setup must be a string"):
        load(tmp_path)


@pytest.mark.parametrize("value", ["fd", "no"])
def test_load_capture(tmp_path: Path, value: str):
    _write(tmp_path, f'[tool.mew]\ncapture = "{value}"\n')
    assert load(tmp_path).capture == value


def test_load_rejects_unknown_capture_mode(tmp_path: Path):
    _write(tmp_path, '[tool.mew]\ncapture = "sys"\n')
    with pytest.raises(ValueError, match="capture must be one of fd, no"):
        load(tmp_path)
