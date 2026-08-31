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
    """How one benchmark's delta was classified by the gate."""

    OK = "ok"
    """Within the active threshold; does not contribute to gate failure."""

    REGRESSED = "regressed"
    """Slower than the active threshold (a rule-supplied one when a rule matches,
    the default otherwise). Fails the gate (exit code 2)."""

    ALLOWED_OVER = "allowed_over"
    """Over the default threshold but inside a rule-supplied one; soft warning."""

    IGNORED = "ignored"
    """Out-of-scope per a matching ``ignore=true`` rule. Listed to keep the allowlist visible."""


@dataclass(frozen=True, slots=True)
class AllowRule:
    """One ``[[tool.mew.regressions.allow]]`` entry.

    Attributes
    ----------
    pattern : str
        ``fnmatch`` pattern matched case-sensitively against the full benchmark name.
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

    def matches(self, name: str) -> bool:
        """Whether ``name`` falls under this rule's pattern."""
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
    """The active gate: a default threshold plus ordered allowlist rules.

    Attributes
    ----------
    default_threshold : float
        Percent slowdown tolerated by benchmarks no rule matches.
    rules : tuple[AllowRule, ...]
        Allowlist rules in file order; the first match wins.
    """

    default_threshold: float
    rules: tuple[AllowRule, ...] = ()

    def find_rule(self, name: str) -> AllowRule | None:
        """The first rule matching ``name``, or ``None`` if none do."""
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
        rule_threshold = rule.threshold if rule is not None else None
        threshold = rule_threshold if rule_threshold is not None else self.default_threshold

        if magnitude > threshold:
            return BenchmarkVerdict(name, delta_pct, Verdict.REGRESSED, rule)
        if rule_threshold is not None and magnitude > self.default_threshold:
            return BenchmarkVerdict(name, delta_pct, Verdict.ALLOWED_OVER, rule)
        return BenchmarkVerdict(name, delta_pct, Verdict.OK, rule)


def _coerce_rule(raw: Mapping[str, object], *, source: Path | str) -> AllowRule:
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{source}: allow rule missing 'pattern'")

    ignore = bool(raw.get("ignore", False))
    threshold = raw.get("threshold")
    if threshold is not None and not isinstance(threshold, int | float):
        raise ValueError(f"{source}: allow rule {pattern!r}: threshold must be a number")

    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            f"{source}: allow rule {pattern!r}: 'reason' is required "
            "(document why this is allowlisted)"
        )

    if not ignore and threshold is None:
        raise ValueError(
            f"{source}: allow rule {pattern!r}: set either ignore=true or threshold=<float>"
        )

    return AllowRule(
        pattern=pattern,
        reason=reason.strip(),
        ignore=ignore,
        threshold=float(threshold) if threshold is not None else None,
    )


def load_config(
    *,
    default_threshold: float,
    path: Path | None = None,
    root: Path | None = None,
) -> RegressionConfig:
    """Build a :class:`RegressionConfig`.

    Parameters
    ----------
    default_threshold : float
        Threshold used when configuration does not override it.
    path : Path, optional
        Explicit TOML file.
    root : Path, optional
        Project root containing ``pyproject.toml``. When omitted, search upward
        from the current directory.

    Returns
    -------
    RegressionConfig
        Parsed threshold and ordered allowlist rules.
    """
    rules: list[AllowRule] = []
    threshold = default_threshold

    source: Path | None = path
    if source is not None and not source.is_file():
        # An explicit path that doesn't exist must not silently gate with
        # defaults; only the implicit pyproject.toml probe may come up empty.
        raise SystemExit(f"regressions config not found: {source}")
    if source is None and root is not None:
        candidate = root / "pyproject.toml"
        source = candidate if candidate.is_file() else None
    elif source is None:
        cwd = Path.cwd().resolve()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / "pyproject.toml"
            if candidate.is_file():
                source = candidate
                break

    if source is not None:
        with source.open("rb") as fh:
            doc = tomllib.load(fh)
        table = doc.get("tool", {}).get("mew", {}).get("regressions", {})
        if "default_threshold" in table:
            threshold = float(table["default_threshold"])
        for raw in table.get("allow", []):
            rules.append(_coerce_rule(raw, source=source))

    return RegressionConfig(default_threshold=threshold, rules=tuple(rules))


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
        ``exit_code`` is 2 if any verdict is :attr:`Verdict.REGRESSED`, else 0.
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
    return "\n".join(lines), (2 if regressed else 0)


def report(
    verdicts: list[BenchmarkVerdict],
    *,
    default_threshold: float,
) -> int:
    """Print the regression panel to stderr and return the exit code.

    :func:`render_panel` with the side effect: stderr keeps the panel out of a
    redirected comparison table.

    Parameters
    ----------
    verdicts : list of BenchmarkVerdict
        One entry per gated benchmark in display order.
    default_threshold : float
        Threshold quoted in the panel header.

    Returns
    -------
    int
        ``2`` if any benchmark regressed, else ``0``. Nothing is printed when
        there is nothing to report.
    """
    text, code = render_panel(verdicts, default_threshold=default_threshold)
    if text:
        print(text, file=sys.stderr)
    return code
