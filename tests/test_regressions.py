"""Tests for `mew.regressions`."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from _helpers import Console, row as _row, write_pair as _write_pair

from mew.compare import compare
from mew.config import load
from mew.regressions import (
    AllowRule,
    BenchmarkVerdict,
    CounterConfig,
    CounterRule,
    RegressionConfig,
    Verdict,
    render_panel,
)

_NOISY = AllowRule(pattern="b*", reason="noisy", threshold=20.0)
_TIGHT = AllowRule(pattern="b*", reason="hot path", threshold=2.0)
_EXEMPT = AllowRule(pattern="b*", reason="flaky", threshold=math.inf)


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
        # An infinite threshold exempts the benchmark however far it moved, and still lists it.
        (_EXEMPT, 50.0, False, Verdict.ALLOWED_OVER),
        (_EXEMPT, 1.0, False, Verdict.OK),
        # Higher-is-better metrics invert the direction.
        (None, -10.0, True, Verdict.REGRESSED),
        (None, 10.0, True, Verdict.OK),
    ],
)
def test_evaluate_verdicts(rule, delta_pct, higher_is_better, verdict) -> None:
    """`evaluate` applies the default threshold, a matching rule, and higher-is-better metrics."""
    cfg = RegressionConfig(default_threshold=5.0, rules=(rule,) if rule else ())
    v = cfg.evaluate("bench_x", delta_pct, higher_is_better=higher_is_better)
    assert v.verdict is verdict
    assert v.rule is rule


def test_first_matching_rule_wins() -> None:
    """When several allow rules match a benchmark, the first one decides the verdict."""
    a = AllowRule(pattern="bench_*", reason="catch-all", threshold=1.0)
    b = AllowRule(pattern="bench_x", reason="more specific", threshold=99.0)
    cfg = RegressionConfig(default_threshold=5.0, rules=(a, b))
    v = cfg.evaluate("bench_x", 50.0)
    assert v.verdict is Verdict.REGRESSED
    assert v.rule is a


def test_load_regressions_from_pyproject(tmp_path: Path) -> None:
    """`[tool.mew.regressions]` in `pyproject.toml` loads the default threshold and allow rules."""
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[tool.mew.regressions]
default-threshold = 7.5

[[tool.mew.regressions.allow]]
pattern = "bench_io_*"
threshold = inf
reason = "depends on disk cache"

[[tool.mew.regressions.allow]]
pattern = "bench_cpu_*"
threshold = 25.0
reason = "noisy on shared runners"
"""
    )
    assert load(tmp_path).regressions == RegressionConfig(
        default_threshold=7.5,
        rules=(
            AllowRule(pattern="bench_io_*", reason="depends on disk cache", threshold=math.inf),
            AllowRule(pattern="bench_cpu_*", reason="noisy on shared runners", threshold=25.0),
        ),
    )


def test_allow_rule_brackets_match_literally() -> None:
    """Square brackets in an allow rule's pattern match literally, not as a character class."""
    rule = AllowRule(pattern="*[algo=bubble]", reason="slow on purpose", threshold=math.inf)
    assert rule.matches("bench.py::sort[algo=bubble]")
    assert not rule.matches("bench.py::sort[algo=merge]")
    # As a character class, `[algo=bubble]` would have matched a single letter.
    assert not rule.matches("bench.py::sorta")


_ALLOW = '[[tool.mew.regressions.allow]]\npattern = "b*"\n'


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_ALLOW + "threshold = inf", "'reason' is required"),
        # A rule's threshold is required, a number, and not negative or NaN.
        (_ALLOW + 'reason = "r"', "'threshold' is required, a percent or inf to exempt"),
        (_ALLOW + 'reason = "r"\nthreshold = "10"', "a non-negative number or inf"),
        (_ALLOW + 'reason = "r"\nthreshold = -1.0', "a non-negative number or inf"),
        (_ALLOW + 'reason = "r"\nthreshold = nan', "a non-negative number or inf"),
        ('[tool.mew.regressions]\nallow = "b*"', "allow must be an array of tables"),
        # The table and its rules both reject typos (test_config covers the rest).
        (
            "[tool.mew.regressions]\ndefault-treshold = 7.5",
            "in [tool.mew.regressions]; did you mean 'default-threshold'?",
        ),
        (
            _ALLOW + 'reason = "r"\ntreshold = 10.0',
            "unknown key 'treshold' in [[tool.mew.regressions.allow]]; did you mean 'threshold'?",
        ),
    ],
)
def test_load_rejects_invalid_regressions_config(tmp_path: Path, body: str, match: str) -> None:
    """An invalid regressions config raises a `ValueError` prefixed with the config file path."""
    # Every row names its error: a TOML syntax error is a ValueError too.
    source = tmp_path / "pyproject.toml"
    source.write_text(body + "\n")
    with pytest.raises(ValueError, match=re.escape(match)) as exc:
        load(tmp_path)
    assert str(exc.value).startswith(f"{source.resolve()}: ")


@pytest.mark.parametrize("threshold", [-1, float("nan"), True, math.inf])
def test_regression_config_rejects_invalid_default_threshold(threshold) -> None:
    """`RegressionConfig` rejects a negative, NaN, boolean or infinite `default_threshold`:
    exempting everything is no gate."""
    with pytest.raises(ValueError, match="default_threshold"):
        RegressionConfig(default_threshold=threshold)


def test_render_panel_exit_codes() -> None:
    """`render_panel` is empty when all pass, exits 3 on a regression, and lists allowed rows."""
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
    assert "(allowlisted: 99.0% threshold; r)" in text
    # An exempt benchmark still shows, so the allowlist stays visible.
    text, code = render_panel(
        [BenchmarkVerdict("x", 50.0, Verdict.ALLOWED_OVER, _EXEMPT)], default_threshold=5.0
    )
    assert "(allowlisted: no threshold; flaky)" in text
    assert text.endswith("0 regression(s), 1 over-but-allowed.")
    assert code == 0


def test_compare_config_allow_rule_passes_the_gate(tmp_path: Path) -> None:
    """An allow rule from `pyproject.toml` lets a delta over the default threshold pass the gate."""
    # +20% is over the default 5% but within the rule's 50%.
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.mew.regressions.allow]]\npattern = "b"\nthreshold = 50.0\nreason = "noisy"\n'
    )
    cfg = load(tmp_path).regressions
    assert compare([base, other], regressions=cfg, console=Console(width=200)) == 0


def test_compare_iterations_metric_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fewer iterations count as a regression, and the displayed delta keeps its sign."""
    # Fewer iterations is slower.
    other, base = _write_pair(
        tmp_path,
        other=[_row("b", 1.0, iterations=800)],
        base=[_row("b", 1.0, iterations=1000)],
    )
    cfg = RegressionConfig(default_threshold=5.0)
    code = compare(
        [base, other],
        metric="iterations",
        regressions=cfg,
        console=Console(width=200),
    )
    assert code == 3
    err = capsys.readouterr().err
    # Display the raw signed delta; only evaluate() flips it for higher-is-better.
    assert "-20.00%" in err


_COUNTER_RULES = CounterConfig(
    rules=(
        CounterRule("l1_misses", True),
        CounterRule("*_misses", False),
        CounterRule("*_hits", True),
    )
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("l1_misses", True),  # a specific rule ahead of its family's
        ("tlb_misses", False),
        ("cache_hits", True),
        ("items_per_second", True),  # a throughput, without a rule
        ("input_size", None),  # no rule: no direction
    ],
)
def test_counter_rules_apply_in_order(name: str, expected: bool | None) -> None:
    """The first matching rule gives a counter its direction."""
    assert _COUNTER_RULES.direction(name) is expected


def test_render_panel_names_the_stricter_rule_a_regression_failed() -> None:
    """A row over a stricter rule's threshold, though under the heading's, shows that rule."""
    verdict = RegressionConfig(rules=(_TIGHT,)).evaluate("bench", 3.0)
    assert verdict.verdict is Verdict.REGRESSED
    text, code = render_panel([verdict], default_threshold=5.0)
    assert "❌ bench   +3.00%   (rule: 2.0% threshold; hot path)" in text
    assert code == 3
