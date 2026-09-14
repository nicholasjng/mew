"""Shared parsing for benchmark run options."""

from __future__ import annotations

from math import isfinite


def parse_min_time(value: str | float) -> str:
    """Validate seconds or a fixed iteration count and return Google Benchmark syntax."""
    text = str(value).strip()
    number = text[:-1] if text.endswith(("s", "x")) else text
    try:
        parsed = float(number)
    except ValueError:
        parsed = float("nan")
    if not isfinite(parsed) or parsed <= 0 or (text.endswith("x") and not number.isdigit()):
        raise ValueError(
            f"min_time must be positive seconds or an iteration count like '100x', got {value!r}"
        )
    return text if text.endswith(("s", "x")) else text + "s"
