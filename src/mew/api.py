"""Public decorators: @benchmark, @parametrize, @product."""

from __future__ import annotations

import inspect
import itertools
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Unpack, cast, overload

if sys.version_info >= (3, 14):
    from annotationlib import get_annotations
else:
    from inspect import get_annotations

from mew._core import TimeUnit
from mew._registry import REGISTRY, Entry
from mew._typing import BenchmarkFn, BenchmarkOptions, TimeUnitStr
from mew.reporter import _CASE_SUFFIX_RE, _OPTION_SUFFIXES_RE

_REGISTERED_ATTR = "__mew_registered__"


_OptionKeys = frozenset(get_annotations(BenchmarkOptions))


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


def _source_file(fn: BenchmarkFn) -> str | None:
    try:
        return inspect.getsourcefile(fn)
    except TypeError:
        return None


def _check_options(options: Mapping[str, Any]) -> None:
    extra = set(options) - _OptionKeys
    if extra:
        raise TypeError(f"unknown option(s): {sorted(extra)}")
    # Validate at decoration time, where the mistake is on screen. GB guards
    # these values with asserts compiled out of release builds, so bad values
    # would otherwise misbehave silently.
    for key in ("iterations", "repetitions", "threads"):
        if (v := options.get(key)) is not None and int(v) < 1:
            raise TypeError(f"{key} must be >= 1, got {v!r}")
    if (v := options.get("min_time")) is not None and float(v) <= 0:
        raise TypeError(f"min_time must be positive, got {v!r}")
    if (v := options.get("min_warmup_time")) is not None and float(v) < 0:
        raise TypeError(f"min_warmup_time must be >= 0, got {v!r}")
    if options.get("threads") is not None and options.get("thread_range") is not None:
        # GB *accumulates* thread counts, so passing both would silently run
        # the union rather than one overriding the other.
        raise TypeError("threads and thread_range are mutually exclusive")

    tr: tuple[int, int] | None = options.get("thread_range")
    if tr is not None:
        try:
            lo, hi = tr
        except (TypeError, ValueError):
            raise TypeError(f"thread_range must be a (min, max) pair, got {tr!r}") from None
        if int(lo) < 1 or int(hi) < int(lo):
            raise TypeError(f"thread_range must satisfy 1 <= min <= max, got {tr!r}")


def _check_addressable(text: str, what: str) -> None:
    """Reject constructs in a user-supplied name/label that collide with mew's
    addressing grammar. Auto-derived names are well-formed by construction; an
    explicit ``name=`` or case label is not."""
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


def _check_name(name: str) -> None:
    _check_addressable(name, "benchmark name")
    # canonical_name strips these when results are read back, so the benchmark
    # would silently regroup under the stripped name in compare/display.
    stripped = _CASE_SUFFIX_RE.sub("", _OPTION_SUFFIXES_RE.sub("", name))
    if stripped != name:
        raise ValueError(
            f"benchmark name {name!r} ends in a Google Benchmark option/case "
            f"suffix, which mew strips when reading results (regrouping it as "
            f"{stripped!r}); pick a name without a reserved trailing suffix"
        )


def _normalize_tags(tags: Iterable[str] | str | None) -> frozenset[str]:
    if not tags:
        return frozenset()
    if isinstance(tags, str):
        return frozenset((tags,))
    return frozenset(tags)


def _mark_registered(fn: BenchmarkFn) -> None:
    if getattr(fn, _REGISTERED_ATTR, False):
        raise RuntimeError(
            f"{fn.__qualname__} is already registered; apply only one of "
            "@benchmark / @parametrize / @product."
        )
    setattr(fn, _REGISTERED_ATTR, True)


def _make_family_trampoline(
    fn: BenchmarkFn,
    cases: list[dict[str, Any]],
    labels: list[str],
    *,
    name: str,
    qualname: str,
) -> BenchmarkFn:
    """Wrap ``fn`` as a Google Benchmark family driven by an index axis.

    The trampoline reads ``state.range(0)`` to look up variant kwargs and label, then dispatches.
    """

    # Plain function (not functools.wraps): the wrapper type would hide __globals__,
    # which BenchmarkFn requires. Copy identity attributes by hand instead.
    def trampoline(state, _fn=fn, _cases=cases, _labels=labels):
        idx = state.range(0)
        state.set_label(_labels[idx])
        return _fn(state, **_cases[idx])

    trampoline.__module__ = fn.__module__
    trampoline.__doc__ = fn.__doc__
    trampoline.__name__ = name
    trampoline.__qualname__ = qualname
    return trampoline


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
    """Register a function as a single benchmark.

    Use :func:`parametrize` or :func:`product` for benchmark families.

    Parameters
    ----------
    fn : BenchmarkFn, optional
        The benchmark function. Passed positionally (bare ``@benchmark``) registers
        immediately; omit to apply options first (``@benchmark(min_time=...)``).
    name : str, optional
        Override the auto-derived ``path/to/file.py::qualname`` registration name.
        Must not contain ``::``, ``[``/``]``, or newlines, or end in a Google
        Benchmark option suffix; these collide with how names are addressed.
    tags : Iterable[str] or str, optional
        Labels used by ``mew run --tag <name>`` for filtering. A single string is one tag.
    **options
        Google Benchmark options; see :class:`~mew._typing.BenchmarkOptions` for the keys.

    Returns
    -------
    BenchmarkFn or Callable[[BenchmarkFn], BenchmarkFn]
        The original function (bare form), or a decorator (called form).

    Raises
    ------
    TypeError
        If ``options`` contains an unknown key or an out-of-range value.
    ValueError
        If ``name`` collides with mew's addressing grammar (see above).
    RuntimeError
        If the same function is already registered via another decorator.

    Examples
    --------
    >>> @mew.benchmark
    ... def bench_sort(state):
    ...     for _ in state:
    ...         sorted([3, 1, 2])
    """
    _check_options(options)
    if name is not None:
        _check_name(name)
    norm_tags = _normalize_tags(tags)

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        # Guard before adding: a failed double-registration must not leave a
        # second entry in the registry.
        _mark_registered(target)
        file = _source_file(target)
        REGISTRY.add(
            Entry(
                name=name or _qualified_name(target, file),
                fn=target,
                file=file,
                options=options,
                tags=norm_tags,
            )
        )
        return target

    if fn is not None:
        return deco(fn)
    return deco


def _register_family(
    target: BenchmarkFn,
    variants: Sequence[dict[str, Any]],
    *,
    name: str | None,
    ids: Sequence[str] | None,
    options: BenchmarkOptions,
    tags: frozenset[str],
) -> BenchmarkFn:
    if ids is not None:
        ids = list(ids)
        if len(ids) != len(variants):
            raise ValueError(f"ids has {len(ids)} entries but parameters has {len(variants)}")
    if not variants:
        raise ValueError("parametrize/product needs at least one case")
    if name is not None:
        _check_name(name)

    # Guard before adding: a failed double-registration must not leave a
    # second entry in the registry.
    _mark_registered(target)
    file = _source_file(target)
    base_name = name or _qualified_name(target, file)
    cases = [dict(kw) for kw in variants]
    labels = list(ids) if ids is not None else [_default_id(kw) for kw in cases]
    # Labels are spliced into `name[label]` addressing, so they carry the same
    # structural constraints as names; ids= overrides a derived label.
    for label in labels:
        _check_addressable(label, "case label")
    if len(set(labels)) != len(labels):
        from collections import Counter

        dupes = sorted(label for label, n in Counter(labels).items() if n > 1)
        # Ambiguous labels break `name[label]` addressing (-k filters, compare
        # merging). Non-scalar values collapse to their type name, so two list
        # cases both label as `data=list` unless ids disambiguate.
        raise ValueError(
            f"duplicate case label(s) {dupes}; pass explicit ids= to disambiguate "
            "(non-scalar parameter values collapse to their type name)"
        )

    trampoline = _make_family_trampoline(
        target,
        cases,
        labels,
        name=target.__name__,
        qualname=target.__qualname__,
    )
    REGISTRY.add(
        Entry(
            name=base_name,
            fn=trampoline,
            file=file,
            options=options,
            tags=tags,
            case_labels=labels,
        )
    )
    return target


def _make_family_decorator(
    variants: Sequence[dict[str, Any]],
    *,
    name: str | None,
    ids: Sequence[str] | None,
    options: BenchmarkOptions,
    tags: frozenset[str],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """The decorator parametrize/product return: register ``target`` as a family."""

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        return _register_family(target, variants, name=name, ids=ids, options=options, tags=tags)

    return deco


def parametrize(
    parameters: Iterable[dict[str, Any]],
    *,
    name: str | None = None,
    ids: Sequence[str] | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Unpack[BenchmarkOptions],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a parametrized benchmark family.

    Registers one benchmark per item in ``parameters``; each variant binds its kwargs
    into the wrapped function and appends a ``[label]`` suffix to the registration name.

    Parameters
    ----------
    parameters : Iterable[dict[str, Any]]
        One dict of kwargs per variant. Snapshotted eagerly, so generators are fine.
    name : str, optional
        Override the auto-derived base name.
    ids : Sequence[str], optional
        Explicit labels (one per variant). Defaults to kwarg-derived labels
        (e.g. ``n=10-algo=merge``).
    tags : Iterable[str] or str, optional
        Labels applied to every variant.
    **options
        Google Benchmark options applied to every variant. Same keys as :func:`benchmark`.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        Decorator that registers the family and returns the original function unchanged.

    Raises
    ------
    ValueError
        If ``ids`` length doesn't match ``parameters``, or a name/label
        collides with mew's addressing grammar (``::``, ``[``/``]``, newlines).
    TypeError
        If ``options`` contains an unknown key or an out-of-range value.
    RuntimeError
        If the same function is already registered via another decorator.

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
    _check_options(options)
    norm_tags = _normalize_tags(tags)
    variants = [dict(p) for p in parameters]  # snapshot, allow generators
    return _make_family_decorator(variants, name=name, ids=ids, options=options, tags=norm_tags)


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
    report_aggregates_only: bool = False,
    threads: int | None = None,
    thread_range: tuple[int, int] | None = None,
    **iterables: Iterable[Any],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a benchmark family from the cartesian product of iterables.

    Registers one benchmark per tuple in the cartesian product over ``**iterables``.

    Parameters
    ----------
    name : str, optional
        Override the auto-derived base name.
    ids : Sequence[str], optional
        Explicit labels (one per cartesian-product tuple).
    tags : Iterable[str] or str, optional
        Labels applied to every variant.
    min_time, min_warmup_time : float, optional
        Per-variant Google Benchmark timing options.
    iterations, repetitions : int, optional
        Per-variant Google Benchmark iteration controls.
    unit : str, optional
        Override Google Benchmark's reported time unit.
    use_real_time, use_manual_time, measure_process_cpu_time : bool
        Flag-style Google Benchmark options.
    report_aggregates_only : bool
        Suppress per-repetition rows when ``repetitions > 1``.
    threads : int, optional
        Run each case with this many threads. Requires a free-threaded interpreter;
        on a GIL build :func:`mew.run` warns and skips threaded
        benchmarks by default. See :class:`~mew._typing.BenchmarkOptions`.
    thread_range : tuple[int, int], optional
        Run each case once per thread count in ``[min, max]`` (powers of two).
        Mutually exclusive with ``threads``; same free-threading requirement.
    **iterables
        Parameter name → iterable of values.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        Decorator that registers the family and returns the original function unchanged.

    Raises
    ------
    TypeError
        If no iterables are supplied, or an option value is illegal
        (e.g. ``threads`` and ``thread_range`` both set).
    ValueError
        If a name/label collides with mew's addressing grammar
        (``::``, ``[``/``]``, newlines).
    RuntimeError
        If the same function is already registered via another decorator.

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
        "report_aggregates_only": report_aggregates_only,
        "threads": threads,
        "thread_range": thread_range,
    }
    options = cast(BenchmarkOptions, {k: v for k, v in local_options.items() if v not in (None, False)})
    _check_options(options)

    norm_tags = _normalize_tags(tags)
    keys = list(iterables.keys())
    value_lists = [list(v) for v in iterables.values()]
    variants = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*value_lists)]
    return _make_family_decorator(variants, name=name, ids=ids, options=options, tags=norm_tags)
