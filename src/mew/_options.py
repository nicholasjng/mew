"""Validate and normalize benchmark and run options.

A wrong type raises :class:`TypeError`, a value out of range :class:`ValueError`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite
from typing import TYPE_CHECKING, Any, cast

from mew._core import TimeUnit
from mew._format import overflow
from mew._typing import BenchmarkOptions
from mew.machine import gil_enabled

if TYPE_CHECKING:
    from mew._registry import Entry

_OPTION_KEYS = BenchmarkOptions.__required_keys__ | BenchmarkOptions.__optional_keys__
_TIME_UNITS = tuple(unit.value for unit in TimeUnit)


def parse_seconds(text: str) -> float:
    """Seconds from a duration: ``"0.5"`` or ``"0.5s"``, ``"500ms"``, or ``"1m"``.

    Raises :class:`ValueError` for any other text; callers check the range.
    """
    text = text.strip()
    if text.endswith("ms"):  # before "s" and "m": "500ms" is neither
        return float(text[:-2]) / 1000
    if text.endswith("m"):
        return float(text[:-1]) * 60
    return float(text.removesuffix("s"))


def parse_min_time(value: str | float) -> str:
    """Convert a minimum time to Google Benchmark's syntax, ``"0.5s"`` or ``"100x"``.

    The time is a duration that :func:`parse_seconds` reads, or a fixed iteration
    count like ``"100x"``.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TypeError(f"min_time must be seconds or a string like '100x', got {value!r}")
    text = str(value).strip()
    if text.endswith("x") and text[:-1].isdigit() and int(text[:-1]) > 0:
        return text
    try:
        seconds = parse_seconds(text)
    except ValueError:
        seconds = float("nan")
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError(
            "min_time must be positive seconds ('0.5', '0.5s', '500ms', '1m') or an "
            f"iteration count like '100x', got {value!r}"
        )
    return f"{seconds}s"


def check_min_warmup_time(value: object) -> None:
    """Reject warm-up seconds that aren't a finite number >= 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"min_warmup_time must be a number of seconds, got {value!r}")
    if not isfinite(value) or value < 0:
        raise ValueError(f"min_warmup_time must be a finite number >= 0, got {value!r}")


def check_count(value: object, name: str) -> None:
    """Reject a count that isn't an integer >= 1; a ``bool`` is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {value!r}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value!r}")


def check_session_tag(tag: object) -> None:
    """Reject a session tag that isn't a non-empty string, or is the reserved ``latest``."""
    if not isinstance(tag, str):
        raise TypeError(f"session_tag must be a string, got {tag!r}")
    if not tag.strip():
        raise ValueError("session_tag must not be empty")
    if tag == "latest":
        raise ValueError(
            "session_tag 'latest' is reserved: `mew compare path@latest` selects the newest session"
        )


def thread_counts(opts: BenchmarkOptions) -> tuple[int, ...]:
    """The thread counts a benchmark asked for; ``(1,)`` when unspecified."""
    v = opts.get("threads")
    if v is None:
        return (1,)
    return (v,) if isinstance(v, int) else tuple(v)


def is_threaded(opts: BenchmarkOptions) -> bool:
    """Whether ``opts`` asks Google Benchmark to spawn more than one worker thread."""
    return max(thread_counts(opts)) > 1


THREADED_ON_GIL = (
    "threaded benchmarks require a free-threaded interpreter; "
    "this interpreter has the GIL enabled, where threaded mode "
    "would deadlock on Google Benchmark's start barrier"
)


def threaded_on_gil(entries: Iterable[Entry]) -> tuple[list[Entry], str]:
    """The threaded benchmarks this build can't run, and the first few names."""
    threaded = [e for e in entries if is_threaded(e.options)] if gil_enabled() else []
    return threaded, ", ".join(e.name for e in threaded[:3]) + overflow(len(threaded), 3)


def check_strict(entries: Iterable[Entry]) -> None:
    """Raise :class:`RuntimeError` if this build can't run some of the threaded ``entries``."""
    threaded, names = threaded_on_gil(entries)
    if threaded:
        raise RuntimeError(
            f"{THREADED_ON_GIL}. Affected: {names}. Run on a free-threaded build, "
            "drop the threads option, or pass strict=False to skip them."
        )


def _normalize_threads(value: Any) -> tuple[int, ...]:
    """Coerce ``threads`` (an int or an iterable of ints) to a tuple of distinct counts.

    Google Benchmark accumulates thread counts, so each count runs the benchmark
    once; duplicates are dropped and the given order is kept.
    """
    if isinstance(value, bool) or not isinstance(value, int | Iterable):
        raise TypeError(f"threads must be an int or an iterable of ints, got {value!r}")
    counts = (value,) if isinstance(value, int) else tuple(dict.fromkeys(value))
    if not counts:
        raise ValueError("threads must name at least one thread count")
    for n in counts:
        check_count(n, "threads")
    return counts


def normalize_options(options: Mapping[str, Any]) -> BenchmarkOptions:
    """Validate benchmark options and normalize them.

    Unset (``None``) options are dropped, ``min_time`` becomes seconds, or
    ``iterations`` for ``"Nx"``, and ``threads`` becomes a tuple.
    """
    extra = set(options) - _OPTION_KEYS
    if extra:
        raise TypeError(f"unknown option(s): {sorted(extra)}")
    opts = {k: v for k, v in options.items() if v is not None}
    # Fail at decoration time: GB guards many of these with asserts compiled out
    # of release builds, so bad values would misbehave silently.
    for key in ("iterations", "repetitions"):
        if key in opts:
            check_count(opts[key], key)
    if "min_warmup_time" in opts:
        check_min_warmup_time(opts["min_warmup_time"])
    if "unit" in opts:
        if not isinstance(opts["unit"], str):
            raise TypeError(f"unit must be a string, got {opts['unit']!r}")
        if opts["unit"] not in _TIME_UNITS:
            units = ", ".join(map(repr, _TIME_UNITS))
            raise ValueError(f"unit must be one of {units}, got {opts['unit']!r}")
    for key in ("use_real_time", "use_manual_time", "measure_process_cpu_time"):
        if key in opts and not isinstance(opts[key], bool):
            raise TypeError(f"{key} must be a bool, got {opts[key]!r}")
    if "threads" in opts:
        opts["threads"] = _normalize_threads(opts["threads"])

    fixed = "iterations"
    if "min_time" in opts:
        min_time = parse_min_time(opts["min_time"])
        if min_time.endswith("x"):
            # "Nx" fixes the iteration count, as it does for mew.run().
            fixed = f"min_time={opts.pop('min_time')!r}"
            if "iterations" in opts:
                raise ValueError(f"{fixed} cannot be combined with iterations")
            opts["iterations"] = int(min_time[:-1])
        else:
            opts["min_time"] = float(min_time[:-1])
    # GB checks these combinations only in debug builds, which abort; release
    # builds silently drop the time budget, or use_real_time for manual time.
    if "iterations" in opts:
        for key in ("min_time", "min_warmup_time"):
            if key in opts:
                raise ValueError(f"{fixed} cannot be combined with {key}")
    if opts.get("use_real_time") and opts.get("use_manual_time"):
        raise ValueError("use_real_time cannot be combined with use_manual_time")
    return cast(BenchmarkOptions, opts)
