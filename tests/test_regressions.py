"""Tests for `mew.regressions`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _helpers import Console, row as _row, write_pair as _write_pair

from mew.compare import compare
from mew.regressions import (
    AllowRule,
    BenchmarkVerdict,
    RegressionConfig,
    Verdict,
    load_config,
    render_panel,
)

_NOISY = AllowRule(pattern="b*", reason="noisy", threshold=20.0)
_TIGHT = AllowRule(pattern="b*", reason="hot path", threshold=2.0)
_IGNORE = AllowRule(pattern="b*", reason="flaky", ignore=True)


@pytest.mark.parametrize(
    ("rule", "delta_pct", "higher_is_better", "verdict"),
    [
        # No rule: the 5% default gates on strict `>`.
        (None, 5.0, False, Verdict.OK),
        (None, 5.001, False, Verdict.REGRESSED),
        # A rule raising the bar to 20%: over the default but within the rule warns,
        # over the rule fails; the allowance is not an unlimited escape hatch.
        (_NOISY, 5.0, False, Verdict.OK),
        (_NOISY, 20.0, False, Verdict.ALLOWED_OVER),
        (_NOISY, 20.001, False, Verdict.REGRESSED),
        # A rule may tighten below the default.
        (_TIGHT, 3.0, False, Verdict.REGRESSED),
        # An ignore rule takes the benchmark out of scope however far it moved.
        (_IGNORE, 50.0, False, Verdict.IGNORED),
        # Higher-is-better metrics invert the direction.
        (None, -10.0, True, Verdict.REGRESSED),
        (None, 10.0, True, Verdict.OK),
    ],
)
def test_evaluate_verdicts(rule, delta_pct, higher_is_better, verdict) -> None:
    cfg = RegressionConfig(default_threshold=5.0, rules=(rule,) if rule else ())
    v = cfg.evaluate("bench_x", delta_pct, higher_is_better=higher_is_better)
    assert v.verdict is verdict
    assert v.rule is rule


def test_first_matching_rule_wins() -> None:
    a = AllowRule(pattern="bench_*", reason="catch-all", ignore=True)
    b = AllowRule(pattern="bench_x", reason="more specific", threshold=99.0)
    cfg = RegressionConfig(default_threshold=5.0, rules=(a, b))
    v = cfg.evaluate("bench_x", 50.0)
    assert v.verdict is Verdict.IGNORED
    assert v.rule is a


def test_load_config_from_pyproject(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[tool.mew.regressions]
default-threshold = 7.5

[[tool.mew.regressions.allow]]
pattern = "bench_io_*"
ignore = true
reason = "depends on disk cache"

[[tool.mew.regressions.allow]]
pattern = "bench_cpu_*"
threshold = 25.0
reason = "noisy on shared runners"
"""
    )
    cfg = load_config(root=tmp_path)
    assert cfg.default_threshold == 7.5
    # An explicit threshold (--regression-threshold) wins over the file.
    assert load_config(default_threshold=2.0, root=tmp_path).default_threshold == 2.0
    assert len(cfg.rules) == 2
    assert cfg.rules[0].ignore is True
    assert cfg.rules[1].threshold == 25.0


def test_allow_rule_brackets_match_literally() -> None:
    rule = AllowRule(pattern="*[algo=bubble]", reason="slow on purpose", ignore=True)
    assert rule.matches("bench.py::sort[algo=bubble]")
    assert not rule.matches("bench.py::sort[algo=merge]")
    # As a character class, `[algo=bubble]` would have matched a single letter.
    assert not rule.matches("bench.py::sorta")


_ALLOW = '[[tool.mew.regressions.allow]]\npattern = "b*"\n'


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_ALLOW + "ignore = true", "'reason' is required"),
        # A rule sets exactly one mode, of the right type and range.
        (_ALLOW + 'reason = "r"', "exactly one of ignore=true or threshold"),
        (_ALLOW + 'reason = "r"\nignore = true\nthreshold = 10.0', "exactly one of"),
        (_ALLOW + 'reason = "r"\nignore = "false"', "ignore must be a boolean"),
        (_ALLOW + 'reason = "r"\nthreshold = -1.0', "finite, non-negative"),
        ('[tool.mew.regressions]\nallow = "b*"', "allow must be an array of tables"),
        # Typos fail, in the table and in its rules alike, and keys are dashed.
        (
            "[tool.mew.regressions]\ndefault-treshold = 7.5",
            "in [tool.mew.regressions]; did you mean 'default-threshold'?",
        ),
        (
            "[tool.mew.regressions]\ndefault_threshold = 7.5",
            "unknown key 'default_threshold' in [tool.mew.regressions]; did you mean 'default-threshold'?",
        ),
        (
            _ALLOW + 'reason = "r"\ntreshold = 10.0',
            "unknown key 'treshold' in [[tool.mew.regressions.allow]]; did you mean 'threshold'?",
        ),
    ],
)
def test_load_config_rejects_invalid_config(tmp_path: Path, body: str, match: str) -> None:
    # Every row names its error: a TOML syntax error is a ValueError too.
    source = tmp_path / "pyproject.toml"
    source.write_text(body + "\n")
    with pytest.raises(ValueError, match=re.escape(match)) as exc:
        load_config(default_threshold=5.0, root=tmp_path)
    assert str(exc.value).startswith(f"{source}: ")


@pytest.mark.parametrize("threshold", [-1, float("nan"), True])
def test_regression_config_rejects_invalid_default_threshold(threshold) -> None:
    with pytest.raises(ValueError, match="default_threshold"):
        RegressionConfig(default_threshold=threshold)


def test_render_panel_exit_codes() -> None:
    rule = AllowRule(pattern="x", reason="r", threshold=99.0)
    text, code = render_panel([BenchmarkVerdict("x", 1.0, Verdict.OK, None)], default_threshold=5.0)
    assert text == ""
    assert code == 0
    text, code = render_panel(
        [BenchmarkVerdict("x", 10.0, Verdict.REGRESSED, None)], default_threshold=5.0
    )
    assert "❌" in text
    assert code == 3
    text, code = render_panel(
        [BenchmarkVerdict("x", 30.0, Verdict.ALLOWED_OVER, rule)], default_threshold=5.0
    )
    assert "⚠️" in text
    assert code == 0
    # Ignored benchmarks still show in the panel, so the allowlist stays visible.
    ignore_rule = AllowRule(pattern="x", reason="flaky", ignore=True)
    text, code = render_panel(
        [BenchmarkVerdict("x", 50.0, Verdict.IGNORED, ignore_rule)], default_threshold=5.0
    )
    assert "✅" in text
    assert "allowlisted: ignored" in text
    assert code == 0


def test_compare_config_allow_rule_passes_the_gate(tmp_path: Path) -> None:
    # +20% is over the default 5% but within the rule's 50%.
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.mew.regressions.allow]]\npattern = "b"\nthreshold = 50.0\nreason = "noisy"\n'
    )
    cfg = load_config(default_threshold=5.0, root=tmp_path)
    assert compare([other, base], regressions=cfg, console=Console(width=200)) == 0


def test_compare_iterations_metric_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Fewer iterations is slower.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 1.0, iterations=800)],
        base=[_row("b", 1.0, iterations=1000)],
    )
    cfg = RegressionConfig(default_threshold=5.0)
    code = compare(
        [other, base],
        metric="iterations",
        regressions=cfg,
        console=Console(width=200),
    )
    assert code == 3
    err = capsys.readouterr().err
    # Display the raw signed delta; only evaluate() flips it for higher-is-better.
    assert "-20.00%" in err
