"""Loading `[tool.mew]` from pyproject.toml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mew.config import Config, load


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body)


# The nearest pyproject.toml anchors the project, with or without a [tool.mew]
# table, so no ancestor of tmp_path can leak its settings in.
@pytest.mark.parametrize("body", ["", "[tool.mew]\n"])
def test_load_defaults_from_the_nearest_pyproject(tmp_path: Path, body: str):
    _write(tmp_path, body)
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    # Defaults live only on the dataclass; the loader just records the root.
    assert load(nested) == Config(project_root=tmp_path.resolve())


def test_load_reads_every_setting(tmp_path: Path):
    _write(
        tmp_path,
        "[tool.mew]\n"
        # A bare string must be one path, not list("perf") == ["p", "e", "r", "f"].
        'benchpaths = "perf"\n'
        'python-files = ["bench_*.py", "*_perf.py"]\n'  # dashed, as TOML spells keys
        'setup = "benchmarks/conf.py"\n'
        'statistic = "gmean"\n'
        'capture = "no"\n'
        # A table mew.regressions reads: known here, not a typo.
        "[tool.mew.regressions]\n"
        "default-threshold = 7.5\n",
    )
    assert load(tmp_path) == Config(
        benchpaths=["perf"],
        python_files=["bench_*.py", "*_perf.py"],
        setup="benchmarks/conf.py",
        statistic="gmean",
        capture="no",
        project_root=tmp_path.resolve(),
    )


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("benchpaths = 42", "benchpaths must be a string or a list of strings"),
        ("statistic = 95", "statistic must be a string"),
        ('statistic = "average"', "[tool.mew] statistic 'average': unknown name"),
        ('capture = "sys"', "capture must be one of fd, no"),
        # A typo would otherwise load the default; the hint keeps the key's spelling.
        ('benchpath = "perf"', "unknown key 'benchpath' in [tool.mew]; did you mean 'benchpaths'?"),
        ('python-file = "*.py"', "did you mean 'python-files'?"),
        (
            'python-files = "a.py"\npython_files = "b.py"',
            "[tool.mew] sets both 'python-files' and 'python_files'",
        ),
    ],
)
def test_load_rejects_malformed_setting(tmp_path: Path, setting: str, message: str):
    _write(tmp_path, f"[tool.mew]\n{setting}\n")
    with pytest.raises(ValueError, match=re.escape(message)):
        load(tmp_path)
