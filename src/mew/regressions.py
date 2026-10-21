"""Regression gating: classify benchmark deltas against thresholds.

A *regression* is a benchmark whose delta against the baseline exceeds a threshold
in the slower direction (larger for ``real_time`` / ``cpu_time``, smaller for
``iterations``). Per-benchmark allowlist rules can ignore a benchmark or raise its
threshold, matched against the full name with ``*`` and ``?`` wildcards.
"""

from __future__ import annotations

import fnmatch
import math
import sys
from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    """A regression gate's classification of one benchmark's delta."""

    OK = "ok"
    """Within the active threshold; does not contribute to gate failure."""

    REGRESSED = "regressed"
    """Slower than the active threshold (a rule-supplied one when a rule matches,
    the default otherwise). Fails the gate (exit code 3)."""

    ALLOWED_OVER = "allowed_over"
    """Over the default threshold but inside a rule-supplied one; soft warning."""

    IGNORED = "ignored"
    """Out-of-scope per a matching ``ignore=true`` rule. Listed to keep the allowlist visible."""


def _check_threshold(value: object, field: str) -> None:
    """Raise unless ``value`` is a finite, non-negative, non-``bool`` number."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be a finite, non-negative number")


@dataclass(frozen=True, slots=True)
class AllowRule:
    """An allowlist rule that exempts benchmarks from the gate or raises their threshold.

    Rules are read from ``[[tool.mew.regressions.allow]]`` entries.

    Attributes
    ----------
    pattern : str
        Pattern matched case-sensitively against the full benchmark name: ``*``
        and ``?`` are wildcards, and brackets match literally, so
        ``"*[algo=bubble]"`` selects that case of every family.
    reason : str
        Why this benchmark is allowlisted. Required, so the file explains itself.
    ignore : bool, default False
        Exempt matching benchmarks from gating entirely.
    threshold : float or None
        Percent threshold replacing the default for matching benchmarks.
        Exactly one of ``ignore`` / ``threshold`` must be set.
    """

    pattern: str
    reason: str
    ignore: bool = False
    threshold: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ignore, bool):
            raise TypeError("ignore must be a boolean")
        if self.threshold is not None:
            _check_threshold(self.threshold, "threshold")
        if self.ignore == (self.threshold is not None):
            raise ValueError("set exactly one of ignore=true or threshold=<float>")

    def matches(self, name: str) -> bool:
        """Whether ``name`` falls under this rule's pattern.

        Parameters
        ----------
        name : str
            A full benchmark name.

        Returns
        -------
        bool
            Whether the pattern matches ``name``.
        """
        # Escape `[` so a case's `[label]` matches literally.
        return fnmatch.fnmatchcase(name, self.pattern.replace("[", "[[]"))


@dataclass(frozen=True, slots=True)
class BenchmarkVerdict:
    """One benchmark's gating outcome.

    Attributes
    ----------
    name : str
        The full benchmark name.
    delta_pct : float
        Signed percent change against the baseline.
    verdict : Verdict
        How the gate classified ``delta_pct``.
    rule : AllowRule or None
        The allowlist rule that matched ``name``, if any.
    """

    name: str
    delta_pct: float
    verdict: Verdict
    rule: AllowRule | None


@dataclass(frozen=True, slots=True)
class RegressionConfig:
    """A regression gate: a default threshold and ordered allowlist rules.

    ``[tool.mew.regressions]`` configures it, as :attr:`mew.config.Config.regressions`.

    Attributes
    ----------
    default_threshold : float, default 5.0
        Percent slowdown tolerated by benchmarks no rule matches.
    rules : tuple[AllowRule, ...]
        Allowlist rules in file order; the first match wins.
    """

    default_threshold: float = 5.0
    rules: tuple[AllowRule, ...] = ()

    def __post_init__(self) -> None:
        _check_threshold(self.default_threshold, "default_threshold")

    def find_rule(self, name: str) -> AllowRule | None:
        """Find the rule that applies to a benchmark.

        Parameters
        ----------
        name : str
            A full benchmark name.

        Returns
        -------
        AllowRule or None
            The first rule matching ``name``, or ``None`` if none do.
        """
        return next((r for r in self.rules if r.matches(name)), None)

    def evaluate(
        self, name: str, delta_pct: float, *, higher_is_better: bool = False
    ) -> BenchmarkVerdict:
        """Classify a benchmark's delta against this config.

        Parameters
        ----------
        name : str
            Full benchmark name (matched against ``rule.pattern``).
        delta_pct : float
            Signed percent change vs baseline. Positive means slower for
            ``real_time`` / ``cpu_time``; for ``iterations`` use ``higher_is_better=True``.
        higher_is_better : bool
            Invert the sign when computing the regression magnitude.

        Returns
        -------
        BenchmarkVerdict
            The verdict, with the matched rule attached (if any).
        """
        rule = self.find_rule(name)
        if rule is not None and rule.ignore:
            return BenchmarkVerdict(name, delta_pct, Verdict.IGNORED, rule)

        # Magnitude in the "worse" direction: slower for time metrics, fewer
        # iters (negative delta) for iterations.
        magnitude = -delta_pct if higher_is_better else delta_pct
        rule_threshold = rule.threshold if rule is not None else None
        threshold = self.default_threshold if rule_threshold is None else rule_threshold
        if magnitude > threshold:
            return BenchmarkVerdict(name, delta_pct, Verdict.REGRESSED, rule)
        # Past the default but within the threshold: only a looser rule allows that.
        verdict = Verdict.ALLOWED_OVER if magnitude > self.default_threshold else Verdict.OK
        return BenchmarkVerdict(name, delta_pct, verdict, rule)


def render_panel(
    verdicts: list[BenchmarkVerdict],
    *,
    default_threshold: float,
) -> tuple[str, int]:
    """Format the regression panel and compute the exit code.

    Parameters
    ----------
    verdicts : list of BenchmarkVerdict
        One entry per gated benchmark in display order.
    default_threshold : float
        Threshold shown in the panel heading.

    Returns
    -------
    (text, exit_code) : tuple[str, int]
        Panel body (empty if nothing to report) and the exit code.
        ``exit_code`` is 3 if any verdict is :attr:`Verdict.REGRESSED`, else 0.
    """
    regressed = [v for v in verdicts if v.verdict is Verdict.REGRESSED]
    allowed_over = [v for v in verdicts if v.verdict is Verdict.ALLOWED_OVER]
    ignored = [v for v in verdicts if v.verdict is Verdict.IGNORED]

    if not (regressed or allowed_over or ignored):
        return "", 0

    lines = [f"Regressions (threshold +{default_threshold:.1f}%):"]
    for v in regressed:
        lines.append(f"  ❌ {v.name}   {v.delta_pct:+.2f}%")
    for v in allowed_over:
        assert v.rule is not None
        lines.append(
            f"  ⚠️  {v.name}   {v.delta_pct:+.2f}%   "
            f"(allowlisted: {v.rule.threshold:.1f}% threshold; {v.rule.reason})"
        )
    for v in ignored:
        assert v.rule is not None
        lines.append(
            f"  ✅ {v.name}   {v.delta_pct:+.2f}%   (allowlisted: ignored; {v.rule.reason})"
        )

    summary = (
        f"{len(regressed)} regression(s), "
        f"{len(allowed_over)} over-but-allowed, "
        f"{len(ignored)} ignored."
    )
    lines.append("")
    lines.append(summary)
    # 3 keeps a failed gate apart from 1 (nothing to compare) and 2 (usage errors).
    return "\n".join(lines), (3 if regressed else 0)


def report(
    verdicts: list[BenchmarkVerdict],
    *,
    default_threshold: float,
) -> int:
    """Print the regression panel to stderr and return the exit code.

    The panel is the text of :func:`render_panel`; nothing is printed when it is
    empty.

    Parameters
    ----------
    verdicts : list of BenchmarkVerdict
        One entry per gated benchmark in display order.
    default_threshold : float
        Threshold quoted in the panel header.

    Returns
    -------
    int
        ``3`` if any benchmark regressed, else ``0``. Nothing is printed when
        there is nothing to report.
    """
    text, code = render_panel(verdicts, default_threshold=default_threshold)
    if text:
        print(text, file=sys.stderr)
    return code
