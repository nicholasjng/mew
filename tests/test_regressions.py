"""Tests for `mew.regressions`."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import Console, row as _row, write_json as _write_json

from mew.compare import compare
from mew.regressions import (
    AllowRule,
    BenchmarkVerdict,
    RegressionConfig,
    Verdict,
    load_config,
    render_panel,
)


def test_evaluate_within_threshold() -> None:
    cfg = RegressionConfig(default_threshold_pct=5.0)
    v = cfg.evaluate("b", 3.0)
    assert v.verdict is Verdict.OK
    assert v.rule is None


def test_evaluate_regressed() -> None:
    cfg = RegressionConfig(default_threshold_pct=5.0)
    assert cfg.evaluate("b", 10.0).verdict is Verdict.REGRESSED


def test_evaluate_ignored_rule() -> None:
    rule = AllowRule(pattern="b*", reason="flaky", ignore=True)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(rule,))
    v = cfg.evaluate("bench_x", 50.0)
    assert v.verdict is Verdict.IGNORED
    assert v.rule is rule


def test_evaluate_per_rule_threshold_under() -> None:
    rule = AllowRule(pattern="b*", reason="noisy", threshold_pct=20.0)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(rule,))
    # Under the rule's raised threshold but over the default: soft warning.
    v = cfg.evaluate("bench_x", 15.0)
    assert v.verdict is Verdict.ALLOWED_OVER
    assert v.rule is rule
    # Under both thresholds: plain OK.
    assert cfg.evaluate("bench_x", 3.0).verdict is Verdict.OK


def test_evaluate_per_rule_threshold_over() -> None:
    rule = AllowRule(pattern="b*", reason="noisy", threshold_pct=20.0)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(rule,))
    # Over even the rule's raised threshold: the allowance is exhausted, so the
    # gate must fail — a raised threshold is not an unlimited escape hatch.
    v = cfg.evaluate("bench_x", 25.0)
    assert v.verdict is Verdict.REGRESSED
    assert v.rule is rule


def test_evaluate_tightened_rule_threshold() -> None:
    # A rule may also tighten the threshold below the default.
    rule = AllowRule(pattern="b*", reason="hot path", threshold_pct=2.0)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(rule,))
    assert cfg.evaluate("bench_x", 3.0).verdict is Verdict.REGRESSED
    assert cfg.evaluate("bench_x", 1.0).verdict is Verdict.OK


def test_evaluate_iterations_higher_is_better() -> None:
    cfg = RegressionConfig(default_threshold_pct=5.0)
    assert cfg.evaluate("b", -10.0, higher_is_better=True).verdict is Verdict.REGRESSED
    assert cfg.evaluate("b", +10.0, higher_is_better=True).verdict is Verdict.OK


def test_first_matching_rule_wins() -> None:
    a = AllowRule(pattern="bench_*", reason="catch-all", ignore=True)
    b = AllowRule(pattern="bench_x", reason="more specific", threshold_pct=99.0)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(a, b))
    v = cfg.evaluate("bench_x", 50.0)
    assert v.verdict is Verdict.IGNORED
    assert v.rule is a


def test_load_config_from_pyproject(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[tool.mew.regressions]
default_threshold_pct = 7.5

[[tool.mew.regressions.allow]]
pattern = "bench_io_*"
ignore = true
reason = "depends on disk cache"

[[tool.mew.regressions.allow]]
pattern = "bench_cpu_*"
threshold_pct = 25.0
reason = "noisy on shared runners"
"""
    )
    cfg = load_config(default_threshold_pct=5.0, path=py)
    assert cfg.default_threshold_pct == 7.5
    assert len(cfg.rules) == 2
    assert cfg.rules[0].ignore is True
    assert cfg.rules[1].threshold_pct == 25.0


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
        load_config(default_threshold_pct=5.0, path=py)


def test_load_config_neither_ignore_nor_threshold(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b*"
reason = "??"
"""
    )
    with pytest.raises(ValueError, match="ignore=true or threshold_pct"):
        load_config(default_threshold_pct=5.0, path=py)


def test_load_config_explicit_missing_path_errors(tmp_path: Path) -> None:
    # A typo'd --regressions-config must not silently gate with defaults.
    with pytest.raises(SystemExit, match="regressions config not found"):
        load_config(default_threshold_pct=5.0, path=tmp_path / "regresions.toml")


def test_render_panel_exit_codes() -> None:
    rule = AllowRule(pattern="x", reason="r", threshold_pct=99.0)
    # Pure OK: no panel, exit 0.
    text, code = render_panel(
        [BenchmarkVerdict("x", 1.0, Verdict.OK, None)], default_threshold_pct=5.0
    )
    assert text == ""
    assert code == 0
    # Regression: panel + exit 2.
    text, code = render_panel(
        [BenchmarkVerdict("x", 10.0, Verdict.REGRESSED, None)], default_threshold_pct=5.0
    )
    assert "❌" in text
    assert code == 2
    # Allowed-over: panel + exit 0.
    text, code = render_panel(
        [BenchmarkVerdict("x", 30.0, Verdict.ALLOWED_OVER, rule)], default_threshold_pct=5.0
    )
    assert "⚠️" in text
    assert code == 0


def test_compare_passes_when_under_threshold(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 102.0)])  # +2%
    cfg = RegressionConfig(default_threshold_pct=5.0)
    code = compare([base, other], regressions=cfg, console=Console(width=200))
    assert code == 0


def test_compare_fails_on_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 120.0)])  # +20%
    cfg = RegressionConfig(default_threshold_pct=5.0)
    code = compare([base, other], regressions=cfg, console=Console(width=200))
    assert code == 2
    err = capsys.readouterr().err
    assert "❌" in err
    assert "b " in err or "  b " in err  # benchmark name appears in panel


def test_compare_config_allow_lifts_threshold(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 120.0)])  # +20%
    py = tmp_path / "regressions.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b"
threshold_pct = 50.0
reason = "noisy"
"""
    )
    cfg = load_config(default_threshold_pct=5.0, path=py)
    code = compare([base, other], regressions=cfg, console=Console(width=200))
    # 20% > 5% default but the rule allows up to 50% — allowed_over → exit 0.
    assert code == 0


def test_compare_config_allow_ignore_skips_gating(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 200.0)])  # +100%
    py = tmp_path / "regressions.toml"
    py.write_text(
        """
[[tool.mew.regressions.allow]]
pattern = "b"
ignore = true
reason = "known-flaky"
"""
    )
    cfg = load_config(default_threshold_pct=5.0, path=py)
    code = compare([base, other], regressions=cfg, console=Console(width=200))
    assert code == 0


def test_compare_iterations_metric_regression(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 1.0, iterations=1000)])
    _write_json(other, [_row("b", 1.0, iterations=800)])  # -20% iters = slower
    cfg = RegressionConfig(default_threshold_pct=5.0)
    code = compare(
        [base, other],
        metric="iterations",
        regressions=cfg,
        console=Console(width=200),
    )
    assert code == 2
