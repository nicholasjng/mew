"""Regression gating for ``mew compare``.

A *regression* is a benchmark whose delta against the baseline exceeds a threshold
in the slower direction (larger for ``real_time`` / ``cpu_time``, smaller for
``iterations``). Per-benchmark allowlist rules can ignore a benchmark or raise its
threshold, matched against the full name via :func:`fnmatch.fnmatchcase`.
"""

from __future__ import annotations

import fnmatch
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Verdict(Enum):
    OK = "ok"
    """Within the active threshold; does not contribute to gate failure."""

    REGRESSED = "regressed"
    """Slower than the default threshold, no allow rule. Fails the gate (exit code 2)."""

    ALLOWED_OVER = "allowed_over"
    """Over the default threshold but inside a rule-supplied one; soft warning."""

    IGNORED = "ignored"
    """Out-of-scope per a matching ``ignore=true`` rule. Listed to keep the allowlist visible."""


@dataclass(frozen=True, slots=True)
class AllowRule:
    pattern: str
    reason: str
    ignore: bool = False
    threshold_pct: float | None = None

    def matches(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name, self.pattern)


@dataclass(frozen=True, slots=True)
class BenchmarkVerdict:
    """Per-benchmark gating outcome carried through the compare pipeline."""

    name: str
    delta_pct: float
    verdict: Verdict
    rule: AllowRule | None


@dataclass(frozen=True, slots=True)
class RegressionConfig:
    default_threshold_pct: float
    rules: tuple[AllowRule, ...] = ()

    def find_rule(self, name: str) -> AllowRule | None:
        for r in self.rules:
            if r.matches(name):
                return r
        return None

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
        threshold = (
            rule.threshold_pct
            if rule is not None and rule.threshold_pct is not None
            else self.default_threshold_pct
        )

        if magnitude <= threshold:
            return BenchmarkVerdict(name, delta_pct, Verdict.OK, rule)
        if rule is not None and rule.threshold_pct is not None:
            return BenchmarkVerdict(name, delta_pct, Verdict.ALLOWED_OVER, rule)
        return BenchmarkVerdict(name, delta_pct, Verdict.REGRESSED, rule)


def _coerce_rule(raw: Mapping[str, object], *, source: Path | str) -> AllowRule:
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{source}: allow rule missing 'pattern'")

    ignore = bool(raw.get("ignore", False))
    threshold = raw.get("threshold_pct")
    if threshold is not None and not isinstance(threshold, int | float):
        raise ValueError(f"{source}: allow rule {pattern!r}: threshold_pct must be a number")

    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"{source}: allow rule {pattern!r}: 'reason' is required (document why this is allowlisted)"
        )

    if not ignore and threshold is None:
        raise ValueError(
            f"{source}: allow rule {pattern!r}: set either ignore=true or threshold_pct=<float>"
        )

    return AllowRule(
        pattern=pattern,
        reason=reason.strip(),
        ignore=ignore,
        threshold_pct=float(threshold) if threshold is not None else None,
    )


def load_config(
    *,
    default_threshold_pct: float,
    path: Path | None = None,
    inline_allows: list[str] | None = None,
) -> RegressionConfig:
    """Build a :class:`RegressionConfig`.

    Reads ``[tool.mew.regressions]`` from ``path`` (or ``pyproject.toml`` in the cwd
    when ``path`` is ``None``). Inline ``--allow`` strings are appended after file
    rules; both lists are searched in order.
    """
    rules: list[AllowRule] = []
    threshold = default_threshold_pct

    source: Path | None = path
    if source is None:
        candidate = Path.cwd() / "pyproject.toml"
        if candidate.is_file():
            source = candidate

    if source is not None and source.is_file():
        with source.open("rb") as fh:
            doc = tomllib.load(fh)
        table = doc.get("tool", {}).get("mew", {}).get("regressions", {})
        if "default_threshold_pct" in table:
            threshold = float(table["default_threshold_pct"])
        for raw in table.get("allow", []):
            rules.append(_coerce_rule(raw, source=source))

    for spec in inline_allows or []:
        rules.append(_parse_inline(spec))

    return RegressionConfig(default_threshold_pct=threshold, rules=tuple(rules))


def _parse_inline(spec: str) -> AllowRule:
    """Parse a ``PATTERN[:PCT]`` string from the CLI.

    Bare pattern → ``ignore=true``; ``PATTERN:5`` → ``threshold_pct=5.0``. Inline
    rules are ad-hoc; persistent allowlisting belongs in ``pyproject.toml``.
    """
    if ":" in spec:
        pattern, _, pct = spec.rpartition(":")
        try:
            threshold = float(pct)
        except ValueError as exc:
            raise SystemExit(f"--allow: bad threshold in {spec!r}: {exc}") from exc
        return AllowRule(
            pattern=pattern,
            reason="(inline --allow)",
            ignore=False,
            threshold_pct=threshold,
        )
    return AllowRule(pattern=spec, reason="(inline --allow)", ignore=True)


def render_panel(
    verdicts: list[BenchmarkVerdict],
    *,
    default_threshold_pct: float,
) -> tuple[str, int]:
    """Format the regression panel and compute the exit code.

    Parameters
    ----------
    verdicts : list of BenchmarkVerdict
        One entry per gated benchmark in display order.

    Returns
    -------
    (text, exit_code) : tuple[str, int]
        Panel body (empty if nothing to report) and the exit code.
        ``exit_code`` is 2 if any verdict is :attr:`Verdict.REGRESSED`, else 0.
    """
    regressed = [v for v in verdicts if v.verdict is Verdict.REGRESSED]
    allowed_over = [v for v in verdicts if v.verdict is Verdict.ALLOWED_OVER]
    ignored = [v for v in verdicts if v.verdict is Verdict.IGNORED]

    if not (regressed or allowed_over or ignored):
        return "", 0

    lines = [f"Regressions (threshold +{default_threshold_pct:.1f}%):"]
    for v in regressed:
        lines.append(f"  ❌ {v.name}   {v.delta_pct:+.2f}%")
    for v in allowed_over:
        assert v.rule is not None
        lines.append(
            f"  ⚠️  {v.name}   {v.delta_pct:+.2f}%   "
            f"(allowlisted: {v.rule.threshold_pct:.1f}% threshold; {v.rule.reason})"
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
    return "\n".join(lines), (2 if regressed else 0)


def report(
    verdicts: list[BenchmarkVerdict],
    *,
    default_threshold_pct: float,
) -> int:
    text, code = render_panel(verdicts, default_threshold_pct=default_threshold_pct)
    if text:
        print(text, file=sys.stderr)
    return code
