"""Public decorators: @benchmark, @parametrize, @product."""

from __future__ import annotations

import inspect
import itertools
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Unpack, cast, overload

from mew._core import TimeUnit
from mew._registry import REGISTRY, Entry
from mew._typing import BenchmarkFn, BenchmarkOptions, TimeUnitStr

_REGISTERED_ATTR = "__mew_registered__"


_OptionKeys = BenchmarkOptions.__required_keys__ | BenchmarkOptions.__optional_keys__


def _format_id_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return repr(value)
    return type(value).__name__


def _default_id(kwargs: dict[str, Any]) -> str:
    return "-".join(f"{k}={_format_id_value(v)}" for k, v in kwargs.items())


def _qualified_name(fn: BenchmarkFn, file: str | None) -> str:
    """Pytest-style nodeid: ``path/to/bench_foo.py::bench_name``.

    Falls back to module + qualname when the source file can't be resolved.
    """
    qualname = fn.__qualname__
    if file:
        try:
            rel = Path(file).resolve().relative_to(Path.cwd())
            return f"{rel}::{qualname}"
        except ValueError:
            return f"{Path(file).name}::{qualname}"
    module = getattr(fn, "__module__", "<unknown>")
    return f"{module}::{qualname}"


def _check_options(options: Mapping[str, Any]) -> None:
    extra = set(options) - _OptionKeys
    if extra:
        raise TypeError(f"unknown option(s): {sorted(extra)}")
    # GB guards these with asserts compiled out of release builds, so bad values
    # would misbehave silently; fail at decoration time instead.
    for key in ("iterations", "repetitions"):
        if (v := options.get(key)) is not None and int(v) < 1:
            raise TypeError(f"{key} must be >= 1, got {v!r}")
    # Negated so NaN fails too.
    if (v := options.get("min_time")) is not None and not float(v) > 0:
        raise TypeError(f"min_time must be positive, got {v!r}")
    if (v := options.get("min_warmup_time")) is not None and not float(v) >= 0:
        raise TypeError(f"min_warmup_time must be >= 0, got {v!r}")
    # GB's Iterations() and MinTime()/MinWarmUpTime() reject each other.
    if options.get("iterations") is not None:
        for key in ("min_time", "min_warmup_time"):
            if options.get(key) is not None:
                raise TypeError(f"iterations cannot be combined with {key}")


def _normalize_threads(value: Any) -> tuple[int, ...]:
    """Coerce ``threads`` (an int or an iterable of ints) to a tuple of distinct counts.

    Google Benchmark accumulates thread counts, so each count runs the benchmark
    once; duplicates are dropped and the given order is kept.
    """
    if isinstance(value, bool) or not isinstance(value, int | Iterable):
        raise TypeError(f"threads must be an int or an iterable of ints, got {value!r}")
    counts = (value,) if isinstance(value, int) else tuple(dict.fromkeys(value))
    if not counts:
        raise TypeError("threads must name at least one thread count")
    for n in counts:
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise TypeError(f"threads must be integers >= 1, got {n!r}")
    return counts


def _normalize_options(options: Mapping[str, Any]) -> BenchmarkOptions:
    """Validate decorator options and snapshot iterables (``threads``) into tuples."""
    _check_options(options)
    normalized = dict(options)
    if (threads := normalized.get("threads")) is not None:
        normalized["threads"] = _normalize_threads(threads)
    return cast(BenchmarkOptions, normalized)


def _check_addressable(text: str, what: str) -> None:
    """Reject names and labels that conflict with selector syntax."""
    if not text.strip():
        raise ValueError(f"{what} must not be empty")
    for bad, why in (
        ("\n", "list output and --stdin selection are line-oriented"),
        ("\r", "list output and --stdin selection are line-oriented"),
        ("::", "'::' separates path::filter selectors and the file prefix"),
        ("[", "'[...]' addresses family cases (name[label])"),
        ("]", "'[...]' addresses family cases (name[label])"),
    ):
        if bad in text:
            raise ValueError(f"{what} {text!r} must not contain {bad!r}; {why}")


def _normalize_tags(tags: Iterable[str] | str | None) -> frozenset[str]:
    if not tags:
        return frozenset()
    if isinstance(tags, str):
        return frozenset((tags,))
    return frozenset(tags)


def _register(
    target: BenchmarkFn,
    fn: BenchmarkFn,
    *,
    name: str | None,
    options: BenchmarkOptions,
    tags: frozenset[str],
    case_labels: list[str] | None = None,
) -> BenchmarkFn:
    """Add ``target`` to the registry as ``fn`` and mark it registered."""
    # Guard before adding: a failed double-registration must not leave a
    # second entry in the registry.
    if getattr(target, _REGISTERED_ATTR, False):
        raise RuntimeError(
            f"{target.__qualname__} is already registered; apply only one of "
            "@benchmark / @parametrize / @product."
        )
    try:
        file = inspect.getsourcefile(target)
    except TypeError:
        file = None
    REGISTRY.add(
        Entry(
            name=name or _qualified_name(target, file),
            fn=fn,
            file=file,
            options=options,
            tags=tags,
            case_labels=case_labels,
        )
    )
    # Marked only once added: a rejected name must leave the function
    # available for a corrected attempt.
    setattr(target, _REGISTERED_ATTR, True)
    return target


@overload
def benchmark(fn: BenchmarkFn, /) -> BenchmarkFn: ...
@overload
def benchmark(
    *,
    name: str | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Unpack[BenchmarkOptions],
) -> Callable[[BenchmarkFn], BenchmarkFn]: ...


def benchmark(
    fn: BenchmarkFn | None = None,
    /,
    *,
    name: str | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Unpack[BenchmarkOptions],
) -> BenchmarkFn | Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a function as a benchmark.

    Use :func:`parametrize` or :func:`product` for a family of cases.

    Parameters
    ----------
    fn : BenchmarkFn, optional
        The function, when used bare (``@benchmark``); omitted when options are
        given (``@benchmark(min_time=...)``).
    name : str, optional
        Replaces the default name, ``path/to/file.py::qualname``. It can't
        contain ``::``, brackets or newlines, which mew's selectors use.
    tags : Iterable[str] or str, optional
        Tags to select the benchmark by (``mew run --tag``); a string is one tag.
    **options
        Google Benchmark options; see :class:`~mew.api.BenchmarkOptions`.

    Returns
    -------
    BenchmarkFn or Callable[[BenchmarkFn], BenchmarkFn]
        The function itself when used bare, otherwise a decorator.

    Raises
    ------
    TypeError
        If an option is unknown or out of range.
    ValueError
        If ``name`` contains ``::``, brackets or a newline.
    RuntimeError
        If another decorator already registered the function.

    Examples
    --------
    >>> @mew.benchmark
    ... def bench_sort(state):
    ...     for _ in state:
    ...         sorted([3, 1, 2])
    """
    norm_options = _normalize_options(options)
    if name is not None:
        _check_addressable(name, "benchmark name")
    norm_tags = _normalize_tags(tags)

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        return _register(target, target, name=name, options=norm_options, tags=norm_tags)

    if fn is not None:
        return deco(fn)
    return deco


def _register_family(
    target: BenchmarkFn,
    cases: list[dict[str, Any]],
    *,
    name: str | None,
    ids: Sequence[str] | None,
    options: BenchmarkOptions,
    tags: frozenset[str],
) -> BenchmarkFn:
    # `cases` holds fresh dicts from parametrize/product, only ever unpacked
    # by the trampoline.
    labels = list(ids) if ids is not None else [_default_id(kw) for kw in cases]
    if len(labels) != len(cases):
        raise ValueError(f"ids has {len(labels)} entries but parameters has {len(cases)}")
    if not cases:
        raise ValueError("parametrize/product needs at least one case")
    if name is not None:
        _check_addressable(name, "benchmark name")

    # Labels are spliced into `name[label]`, so they share the name constraints.
    for label in labels:
        _check_addressable(label, "case label")
    if len(set(labels)) != len(labels):
        dupes = sorted(label for label, n in Counter(labels).items() if n > 1)
        # Ambiguous labels break `name[label]` addressing (-k filters, compare merging).
        raise ValueError(
            f"duplicate case label(s) {dupes}; pass explicit ids= to disambiguate "
            "(non-scalar parameter values collapse to their type name)"
        )

    # The runner registers each case on its own, its index as the arg (`state.range(0)`).
    def trampoline(state, _fn=target, _cases=cases, _labels=labels):
        idx = state.range(0)
        # A default the body may override; the registered name carries the case.
        state.set_label(_labels[idx])
        return _fn(state, **_cases[idx])

    trampoline.__module__ = target.__module__
    trampoline.__doc__ = target.__doc__
    trampoline.__name__ = target.__name__
    trampoline.__qualname__ = target.__qualname__
    return _register(target, trampoline, name=name, options=options, tags=tags, case_labels=labels)


def parametrize(
    parameters: Iterable[dict[str, Any]],
    *,
    name: str | None = None,
    ids: Sequence[str] | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Unpack[BenchmarkOptions],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a family of benchmark cases, one per parameter dict.

    Each case calls the function with its dict as keyword arguments, and is
    named after the family plus ``[label]``.

    Parameters
    ----------
    parameters : Iterable[dict[str, Any]]
        One dict of keyword arguments per case; read once, so a generator works.
    name : str, optional
        Replaces the family's default name.
    ids : Sequence[str], optional
        One label per case, instead of labels made from the values
        (``n=10-algo=merge``).
    tags : Iterable[str] or str, optional
        Tags for every case.
    **options
        Google Benchmark options for every case, as for :func:`benchmark`.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        A decorator that registers the family and returns the function unchanged.

    Raises
    ------
    ValueError
        If there are no cases, ``ids`` doesn't have one label per case, two
        labels are equal, or a name or label contains ``::``, brackets or a
        newline.
    TypeError
        If an option is unknown or out of range.
    RuntimeError
        If another decorator already registered the function.

    Examples
    --------
    >>> @mew.parametrize([
    ...     {"n": 10, "algo": "merge"},
    ...     {"n": 100, "algo": "quick"},
    ... ], min_time=0.05, tags=("sort",))
    ... def bench_sort(state, n, algo):
    ...     data = list(range(n, 0, -1))
    ...     for _ in state:
    ...         sorted(data)
    """
    norm_options = _normalize_options(options)
    norm_tags = _normalize_tags(tags)
    cases = [dict(p) for p in parameters]  # snapshot, allow generators
    return lambda target: _register_family(
        target, cases, name=name, ids=ids, options=norm_options, tags=norm_tags
    )


def product(
    *,
    name: str | None = None,
    ids: Sequence[str] | None = None,
    tags: Iterable[str] | str | None = None,
    min_time: float | None = None,
    min_warmup_time: float | None = None,
    iterations: int | None = None,
    repetitions: int | None = None,
    unit: TimeUnitStr | TimeUnit | None = None,
    use_real_time: bool = False,
    use_manual_time: bool = False,
    measure_process_cpu_time: bool = False,
    threads: int | Iterable[int] | None = None,
    **iterables: Iterable[Any],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a family of benchmark cases, one per combination of values.

    The cases are the cartesian product of ``**iterables``. The Google
    Benchmark options are as for :func:`benchmark`, named here because
    ``**iterables`` takes every other keyword argument.

    Parameters
    ----------
    name : str, optional
        Replaces the family's default name.
    ids : Sequence[str], optional
        One label per combination, instead of labels made from the values.
    tags : Iterable[str] or str, optional
        Tags for every case.
    min_time, min_warmup_time : float, optional
        Seconds to measure and to warm up each case.
    iterations, repetitions : int, optional
        A fixed iteration count, which excludes the two times above, and how
        many runs per case.
    unit : str or TimeUnit, optional
        The time unit results are reported in.
    use_real_time, use_manual_time, measure_process_cpu_time : bool
        Which clock each case's time comes from; ``use_real_time`` and
        ``use_manual_time`` exclude each other.
    threads : int or Iterable[int], optional
        Threads per case, or one run per count. Needs a free-threaded build;
        on a GIL build :func:`mew.run` skips threaded benchmarks.
    **iterables
        Each parameter's values, by name.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        A decorator that registers the family and returns the function unchanged.

    Raises
    ------
    TypeError
        If no iterables are given, or an option is out of range.
    ValueError
        If two labels are equal, or a name or label contains ``::``, brackets
        or a newline.
    RuntimeError
        If another decorator already registered the function.

    Examples
    --------
    >>> @mew.product(n=[10, 100], algo=["merge", "quick"], tags=("sort",))
    ... def bench_sort(state, n, algo):
    ...     ...
    """
    if not iterables:
        raise TypeError("@product needs at least one iterable kwarg")

    local_options = {
        "min_time": min_time,
        "min_warmup_time": min_warmup_time,
        "iterations": iterations,
        "repetitions": repetitions,
        "unit": unit,
        "use_real_time": use_real_time,
        "use_manual_time": use_manual_time,
        "measure_process_cpu_time": measure_process_cpu_time,
        "threads": threads,
    }
    options = _normalize_options(
        {k: v for k, v in local_options.items() if v is not None and v is not False}
    )

    norm_tags = _normalize_tags(tags)
    keys = list(iterables.keys())
    value_lists = [list(v) for v in iterables.values()]
    cases = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*value_lists)]
    return lambda target: _register_family(
        target, cases, name=name, ids=ids, options=options, tags=norm_tags
    )
