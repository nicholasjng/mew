"""Format times, byte counts, counters and truncated lists as text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NS_PER_UNIT = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}


def to_ns(value: float, unit: str | None) -> float:
    """Normalize a ``(value, unit)`` pair to nanoseconds.

    Compared files can declare different units (e.g. ``--benchmark_time_unit=us``),
    so delta/speedup math must go through this, not raw ``sample.value``.
    """
    return value * NS_PER_UNIT.get(unit or "ns", 1.0)


def scale_time(value: float, unit: str | None) -> tuple[float, str]:
    """Rescale a ``(value, unit)`` pair to the largest of s/ms/us/ns that keeps it >= 1.

    For example, ``(6995135790.99, "ns")`` becomes ``(7.00, "s")``.
    """
    ns = to_ns(value, unit)
    for threshold, out_unit in ((1e9, "s"), (1e6, "ms"), (1e3, "us")):
        if abs(ns) >= threshold:
            return ns / threshold, out_unit
    return ns, "ns"


def fmt_time(value: float, unit: str | None) -> str:
    scaled, out_unit = scale_time(value, unit)
    return f"{scaled:.2f} {out_unit}"


def fmt_bytes(n: int) -> str:
    for threshold, unit in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


_BIG_SI = ("k", "M", "G", "T", "P", "E", "Z", "Y")
_BIG_IEC = ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi", "Yi")
_SMALL_SI = ("m", "u", "n", "p", "f", "a", "z", "y")


def human_readable(value: float, one_k: int, exponent: bool) -> str:
    """Google Benchmark's ``HumanReadableNumber`` (string_util.cc), one digit of precision."""
    sign, v, power = "-" if value < 0 else "", abs(value), 0
    if v > one_k - 1:
        scaled = v
        for i in range(len(_BIG_SI)):
            scaled /= one_k
            if scaled <= one_k - 1:
                v, power = scaled, i + 1
                break
    elif v < 0.01:  # values in [0.01, 1) print as they are
        scaled = v
        for i in range(len(_SMALL_SI)):
            scaled *= one_k
            if scaled >= 1:
                v, power = scaled, -(i + 1)
                break
    mantissa = f"{sign}{v:g}"
    if exponent:
        return f"{mantissa}*2^{power * 10}" if one_k == 1024 else f"{mantissa}e{power * 3}"
    if power > 0:
        return mantissa + (_BIG_IEC if one_k == 1024 else _BIG_SI)[power - 1]
    return mantissa + (_SMALL_SI[-power - 1] if power < 0 else "")


def fmt_counter(value: float, fmt: Mapping[str, Any] | None) -> str:
    """A counter value as Google Benchmark's console shows it, e.g. ``1.2Gi/s``.

    ``fmt`` is the counter's ``format`` record from a result row, if it has one.
    """
    fmt = fmt or {}
    text = human_readable(value, fmt.get("one_k", 1000), fmt.get("exponent", False))
    if fmt.get("rate"):
        text += "s" if fmt.get("inverted") else "/s"
    return text


def overflow(total: int, shown: int) -> str:
    """`` (+N more)`` when a message previewed only ``shown`` of ``total`` items."""
    return f" (+{total - shown} more)" if total > shown else ""
