"""Tests for `mew.regressions`."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mew._console import Terminal
from mew.compare import compare
from mew.regressions import (
    AllowRule,
    BenchmarkVerdict,
    RegressionConfig,
    Verdict,
    _parse_inline,
    load_config,
    render_panel,
)


def _row(name: str, real_time: float, **extra) -> dict:
    return {
        "name": name,
        "real_time": real_time,
        "cpu_time": real_time,
        "iterations": 1000,
        "time_unit": "ns",
        "aggregate_name": "",
        **extra,
    }


def _write_json(path: Path, benches: list[dict]) -> None:
    path.write_text(json.dumps({"context": {}, "benchmarks": benches}))


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
    assert cfg.evaluate("bench_x", 15.0).verdict is Verdict.OK


def test_evaluate_per_rule_threshold_over() -> None:
    rule = AllowRule(pattern="b*", reason="noisy", threshold_pct=20.0)
    cfg = RegressionConfig(default_threshold_pct=5.0, rules=(rule,))
    v = cfg.evaluate("bench_x", 25.0)
    assert v.verdict is Verdict.ALLOWED_OVER
    assert v.rule is rule


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


def test_inline_allow_parses_threshold() -> None:
    rule = _parse_inline("bench_x:20")
    assert rule.pattern == "bench_x"
    assert rule.threshold_pct == 20.0
    assert rule.ignore is False


def test_inline_allow_bare_pattern_ignores() -> None:
    rule = _parse_inline("bench_x")
    assert rule.ignore is True
    assert rule.threshold_pct is None


def test_inline_allow_bad_threshold() -> None:
    with pytest.raises(SystemExit, match="bad threshold"):
        _parse_inline("bench_x:not_a_number")


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
    code = compare(
        [base, other], regressions=cfg, console=Terminal(file=io.StringIO(), width=200, color=False)
    )
    assert code == 0


def test_compare_fails_on_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 120.0)])  # +20%
    cfg = RegressionConfig(default_threshold_pct=5.0)
    code = compare(
        [base, other], regressions=cfg, console=Terminal(file=io.StringIO(), width=200, color=False)
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "❌" in err
    assert "b " in err or "  b " in err  # benchmark name appears in panel


def test_compare_inline_allow_lifts_threshold(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 120.0)])  # +20%
    cfg = load_config(default_threshold_pct=5.0, inline_allows=["b:50"])
    code = compare(
        [base, other], regressions=cfg, console=Terminal(file=io.StringIO(), width=200, color=False)
    )
    # 20% > 5% default but rule allows up to 50% — allowed_over → exit 0.
    assert code == 0


def test_compare_inline_allow_ignore_skips_gating(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    _write_json(base, [_row("b", 100.0)])
    _write_json(other, [_row("b", 200.0)])  # +100%
    cfg = load_config(default_threshold_pct=5.0, inline_allows=["b"])  # ignore
    code = compare(
        [base, other], regressions=cfg, console=Terminal(file=io.StringIO(), width=200, color=False)
    )
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
        console=Terminal(file=io.StringIO(), width=200, color=False),
    )
    assert code == 2
