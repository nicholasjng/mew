"""Loading `[tool.mew]` from pyproject.toml."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mew.config import Config, load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(textwrap.dedent(body))


# The nearest pyproject.toml anchors the project, with or without a [tool.mew]
# table, so no ancestor of tmp_path can leak its settings in.
@pytest.mark.parametrize("body", ["", "[tool.mew]\n"])
def test_load_defaults_from_the_nearest_pyproject(tmp_path: Path, body: str):
    _write(tmp_path, body)
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    # Defaults live only on the dataclass; the loader just records the root.
    assert load(nested) == Config(project_root=tmp_path.resolve())


@pytest.mark.parametrize(
    ("setting", "field", "expected"),
    [
        ('benchpaths = ["perf", "slow"]', "benchpaths", ["perf", "slow"]),
        # A bare TOML string must be one path, not list("perf") == ["p","e","r","f"].
        ('benchpaths = "perf"', "benchpaths", ["perf"]),
        ('python-files = "bench_*.py"', "python_files", ["bench_*.py"]),
        ('statistic = "gmean"', "statistic", "gmean"),
        ('setup = "benchmarks/conf.py"', "setup", "benchmarks/conf.py"),
        ('capture = "fd"', "capture", "fd"),
        ('capture = "no"', "capture", "no"),
    ],
)
def test_load_setting(tmp_path: Path, setting: str, field: str, expected):
    _write(tmp_path, f"[tool.mew]\n{setting}\n")
    assert getattr(load(tmp_path), field) == expected


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("benchpaths = 42", "benchpaths must be a string or a list of strings"),
        ("statistic = 95", "statistic must be a string"),
        ("setup = 3", "setup must be a string"),
        ('capture = "sys"', "capture must be one of fd, no"),
    ],
)
def test_load_rejects_malformed_setting(tmp_path: Path, setting: str, message: str):
    _write(tmp_path, f"[tool.mew]\n{setting}\n")
    with pytest.raises(ValueError, match=message):
        load(tmp_path)
