"""Format times, byte counts and truncated lists as text."""

from __future__ import annotations

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


def overflow(total: int, shown: int) -> str:
    """`` (+N more)`` when a message previewed only ``shown`` of ``total`` items."""
    return f" (+{total - shown} more)" if total > shown else ""
