"""Loading `[tool.mew]` from pyproject.toml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mew.config import Config, load
from mew.regressions import CounterConfig, CounterRule, RegressionConfig


def _write(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(body)


# The nearest pyproject.toml anchors the project, with or without a [tool.mew]
# table, so no ancestor of tmp_path can leak its settings in.
@pytest.mark.parametrize("body", ["", "[tool.mew]\n"])
def test_load_defaults_from_the_nearest_pyproject(tmp_path: Path, body: str):
    """`load` from a nested directory gives the defaults, rooted at the nearest pyproject.toml."""
    _write(tmp_path, body)
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    # Defaults live only on the dataclass; the loader just records the root.
    assert load(nested) == Config(project_root=tmp_path.resolve())


def test_load_reads_every_setting(tmp_path: Path):
    """Each `[tool.mew]` key, `[tool.mew.regressions]` included, loads into its `Config` field."""
    _write(
        tmp_path,
        "[tool.mew]\n"
        # A bare string must be one path, not list("perf") == ["p", "e", "r", "f"].
        'benchpaths = "perf"\n'
        'python-files = ["bench_*.py", "*_perf.py"]\n'
        'setup = "benchmarks/conf.py"\n'
        'statistic = "gmean"\n'
        "[tool.mew.regressions]\n"
        "default-threshold = 7.5\n"
        "[[tool.mew.counters]]\n"
        'pattern = "*_misses"\n'
        "higher-is-better = false\n"
        "[[tool.mew.counters]]\n"
        'pattern = "*_per_second"\n'
        "higher-is-better = true\n",
    )
    assert load(tmp_path) == Config(
        benchpaths=["perf"],
        python_files=["bench_*.py", "*_perf.py"],
        setup="benchmarks/conf.py",
        statistic="gmean",
        regressions=RegressionConfig(default_threshold=7.5),
        counters=CounterConfig(
            rules=(CounterRule("*_misses", False), CounterRule("*_per_second", True))
        ),
        project_root=tmp_path.resolve(),
    )


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("benchpaths = 42", "benchpaths must be a string or a list of strings"),
        ("statistic = 95", "statistic must be a string"),
        ('statistic = "average"', "[tool.mew] statistic 'average': unknown name"),
        # A typo would otherwise load the default.
        ('benchpath = "perf"', "unknown key 'benchpath' in [tool.mew]; did you mean 'benchpaths'?"),
        # Keys are dashed, as TOML spells them.
        (
            'python_files = "*.py"',
            "unknown key 'python_files' in [tool.mew]; did you mean 'python-files'?",
        ),
        # Not TOML at all.
        ("benchpaths = ", "(at line 2"),
        ("[tool.mew.counters]\n", "[tool.mew] counters must be an array of tables"),
        ("[[tool.mew.counters]]\nhigher-is-better = true", "counter rule missing 'pattern'"),
        # A rule's purpose is the direction.
        (
            '[[tool.mew.counters]]\npattern = "hits"',
            "counter rule 'hits': 'higher-is-better' is required",
        ),
        (
            '[[tool.mew.counters]]\npattern = "hits"\nhigher-is-better = "yes"',
            "counter rule 'hits': higher-is-better must be a boolean",
        ),
        (
            '[[tool.mew.counters]]\npattern = "hits"\nhigher_is_better = true',
            (
                "unknown key 'higher_is_better' in [[tool.mew.counters]]; "
                "did you mean 'higher-is-better'?"
            ),
        ),
    ],
)
def test_load_rejects_malformed_setting(tmp_path: Path, setting: str, message: str):
    """A wrong-typed, unknown or unparsable setting raises `ValueError` led by the file's path."""
    _write(tmp_path, f"[tool.mew]\n{setting}\n")
    with pytest.raises(ValueError, match=re.escape(message)) as exc:
        load(tmp_path)
    # The message names the file.
    assert str(exc.value).startswith(f"{(tmp_path / 'pyproject.toml').resolve()}: ")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("tool = 1\n", "[tool] must be a table"),
        ("[tool]\nmew = 1\n", "[tool.mew] must be a table"),
        # Would otherwise load as all defaults.
        ("[tool]\nmew = []\n", "[tool.mew] must be a table"),
        ("[tool.mew]\nregressions = 1\n", "[tool.mew.regressions] must be a table"),
    ],
)
def test_load_rejects_a_table_that_is_not_one(tmp_path: Path, body: str, message: str):
    """A `tool` or `tool.mew` that isn't a table raises `ValueError`, not a crash."""
    _write(tmp_path, body)
    with pytest.raises(ValueError, match=re.escape(message)):
        load(tmp_path)


def test_load_skips_a_directory_named_pyproject_toml(tmp_path: Path):
    """Only a file is a pyproject.toml; the search goes on past a directory of that name."""
    _write(tmp_path, '[tool.mew]\nstatistic = "min"\n')
    (tmp_path / "sub" / "pyproject.toml").mkdir(parents=True)
    assert load(tmp_path / "sub").statistic == "min"
