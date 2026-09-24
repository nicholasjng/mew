"""Tests for `mew.regressions`."""

from __future__ import annotations

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
        (None, 3.0, False, Verdict.OK),
        (None, 5.0, False, Verdict.OK),
        (None, 5.001, False, Verdict.REGRESSED),
        (None, 10.0, False, Verdict.REGRESSED),
        # A rule raising the bar to 20%: over the default but within the rule warns,
        # over the rule fails; the allowance is not an unlimited escape hatch.
        (_NOISY, 3.0, False, Verdict.OK),
        (_NOISY, 5.0, False, Verdict.OK),
        (_NOISY, 15.0, False, Verdict.ALLOWED_OVER),
        (_NOISY, 20.0, False, Verdict.ALLOWED_OVER),
        (_NOISY, 20.001, False, Verdict.REGRESSED),
        (_NOISY, 25.0, False, Verdict.REGRESSED),
        # A rule may tighten below the default.
        (_TIGHT, 1.0, False, Verdict.OK),
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
default_threshold = 7.5

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
    cfg = load_config(default_threshold=5.0, root=tmp_path)
    assert cfg.default_threshold == 7.5
    assert len(cfg.rules) == 2
    assert cfg.rules[0].ignore is True
    assert cfg.rules[1].threshold == 25.0


def test_load_config_missing_reason_rejected(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b*"
ignore = true
"""
    )
    with pytest.raises(ValueError, match="reason"):
        load_config(default_threshold=5.0, root=tmp_path)


def test_load_config_neither_ignore_nor_threshold(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b*"
reason = "??"
"""
    )
    with pytest.raises(ValueError, match="exactly one of ignore=true or threshold"):
        load_config(default_threshold=5.0, root=tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        'ignore = "false"',
        "ignore = true\nthreshold = 10.0",
        "threshold = true",
        "threshold = -1.0",
        "threshold = nan",
    ],
)
def test_load_config_rejects_invalid_rule_modes(tmp_path: Path, body: str) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        f"""\n[[tool.mew.regressions.allow]]\npattern = "b*"\nreason = "invalid"\n{body}\n"""
    )
    with pytest.raises(ValueError):
        load_config(default_threshold=5.0, root=tmp_path)


def test_load_config_rejects_non_array_allow_table(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text('[tool.mew.regressions]\nallow = "b*"\n')
    with pytest.raises(ValueError, match="allow must be an array of tables"):
        load_config(default_threshold=5.0, root=tmp_path)


@pytest.mark.parametrize("threshold", [-1, float("nan"), float("inf"), True])
def test_regression_config_rejects_invalid_default_threshold(threshold) -> None:
    with pytest.raises(ValueError, match="default_threshold"):
        RegressionConfig(default_threshold=threshold)


def test_render_panel_exit_codes() -> None:
    rule = AllowRule(pattern="x", reason="r", threshold=99.0)
    # Pure OK: no panel, exit 0.
    text, code = render_panel([BenchmarkVerdict("x", 1.0, Verdict.OK, None)], default_threshold=5.0)
    assert text == ""
    assert code == 0
    # Regression: panel + exit 2.
    text, code = render_panel(
        [BenchmarkVerdict("x", 10.0, Verdict.REGRESSED, None)], default_threshold=5.0
    )
    assert "❌" in text
    assert code == 2
    # Allowed-over: panel + exit 0.
    text, code = render_panel(
        [BenchmarkVerdict("x", 30.0, Verdict.ALLOWED_OVER, rule)], default_threshold=5.0
    )
    assert "⚠️" in text
    assert code == 0
    # Ignored: panel (visible in the allowlist) + exit 0.
    ignore_rule = AllowRule(pattern="x", reason="flaky", ignore=True)
    text, code = render_panel(
        [BenchmarkVerdict("x", 50.0, Verdict.IGNORED, ignore_rule)], default_threshold=5.0
    )
    assert "✅" in text
    assert "allowlisted: ignored" in text
    assert code == 0


def test_compare_passes_when_under_threshold(tmp_path: Path) -> None:
    # +2%:
    other, base = _write_pair(tmp_path, other=[_row("b", 102.0)], base=[_row("b", 100.0)])
    cfg = RegressionConfig(default_threshold=5.0)
    code = compare([other, base], regressions=cfg, console=Console(width=200))
    assert code == 0


def test_compare_fails_on_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # +20%:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    cfg = RegressionConfig(default_threshold=5.0)
    code = compare([other, base], regressions=cfg, console=Console(width=200))
    assert code == 2
    err = capsys.readouterr().err
    assert "❌ b   +20.00%" in err  # exact panel line: name + signed delta


def test_compare_config_allow_lifts_threshold(tmp_path: Path) -> None:
    # +20%:
    other, base = _write_pair(tmp_path, other=[_row("b", 120.0)], base=[_row("b", 100.0)])
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b"
threshold = 50.0
reason = "noisy"
"""
    )
    cfg = load_config(default_threshold=5.0, root=tmp_path)
    code = compare([other, base], regressions=cfg, console=Console(width=200))
    # 20% > 5% default but the rule allows up to 50% — allowed_over → exit 0.
    assert code == 0


def test_compare_config_allow_ignore_skips_gating(tmp_path: Path) -> None:
    # +100%:
    other, base = _write_pair(tmp_path, other=[_row("b", 200.0)], base=[_row("b", 100.0)])
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b"
ignore = true
reason = "known-flaky"
"""
    )
    cfg = load_config(default_threshold=5.0, root=tmp_path)
    code = compare([other, base], regressions=cfg, console=Console(width=200))
    assert code == 0


def test_compare_iterations_metric_regression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # -20% iters = slower:
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
    assert code == 2
    err = capsys.readouterr().err
    # The displayed delta must stay signed -20.00% (raw, not the higher-is-better
    # magnitude) — only evaluate()'s internal magnitude flips the sign.
    assert "-20.00%" in err
