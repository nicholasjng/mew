"""Public decorators: @benchmark, @parametrize, @product."""

from __future__ import annotations

import functools
import inspect
import itertools
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Unpack, overload

if sys.version_info >= (3, 14):
    from annotationlib import get_annotations
else:
    from inspect import get_annotations

from mew._core import TimeUnit
from mew._registry import REGISTRY, Entry
from mew._typing import BenchmarkFn, BenchmarkOptions, TimeUnitStr

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
    """Pytest-style nodeid: `path/to/bench_foo.py::bench_name`.

    Falls back to the function's module + qualname when the source file
    can't be resolved (REPL, exec'd string, frozen module).
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


def _check_options(options: Mapping[str, object]) -> None:
    extra = set(options) - _OptionKeys
    if extra:
        raise TypeError(f"unknown option(s): {sorted(extra)}")


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
    """Wrap `fn` as a Google Benchmark family driven by an index axis.

    The returned trampoline reads `state.range(0)` (the per-instance index
    Google Benchmark assigns via `.dense_range(0, N-1)`), looks up the real
    kwargs in `cases`, attaches the human-readable label, and dispatches.
    """

    @functools.wraps(fn, assigned=("__module__", "__doc__"))
    def trampoline(state, _fn=fn, _cases=cases, _labels=labels):
        idx = state.range(0)
        state.set_label(_labels[idx])
        return _fn(state, **_cases[idx])

    trampoline.__name__ = name
    trampoline.__qualname__ = qualname
    return trampoline


# ---------- @benchmark ------------------------------------------------------


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

    Use ``@parametrize`` or ``@product`` for benchmark families.

    Parameters
    ----------
    fn : BenchmarkFn, optional
        The benchmark function. When passed positionally (bare ``@benchmark``),
        registration happens immediately. Omit to apply options first
        (``@benchmark(min_time=...)``).
    name : str, optional
        Override the auto-derived ``path/to/file.py::qualname`` registration
        name.
    tags : Iterable[str] or str, optional
        Labels used by ``mew run --tag <name>`` for filtering. A single string
        is treated as one tag.
    **options
        Google Benchmark options: ``min_time``, ``min_warmup_time``,
        ``iterations``, ``repetitions``, ``unit``, ``use_real_time``,
        ``use_manual_time``, ``measure_process_cpu_time``,
        ``report_aggregates_only``.

    Returns
    -------
    BenchmarkFn or Callable[[BenchmarkFn], BenchmarkFn]
        The original function (bare form) or a decorator that takes one (called form).

    Raises
    ------
    TypeError
        If ``options`` contains an unknown key.
    RuntimeError
        If the same function is already registered via another decorator.
    """
    _check_options(options)
    norm_tags = _normalize_tags(tags)

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        file = _source_file(target)
        REGISTRY.add(
            Entry(
                name=name or _qualified_name(target, file),
                fn=target,
                module=getattr(target, "__module__", None),
                file=file,
                options=options,
                tags=norm_tags,
            )
        )
        _mark_registered(target)
        return target

    if fn is not None:
        return deco(fn)
    return deco


# ---------- @parametrize / @product -----------------------------------------


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

    file = _source_file(target)
    base_name = name or _qualified_name(target, file)
    module = getattr(target, "__module__", None)
    cases = [dict(kw) for kw in variants]
    labels = list(ids) if ids is not None else [_default_id(kw) for kw in cases]

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
            module=module,
            file=file,
            options=options,
            tags=tags,
            case_labels=labels,
        )
    )
    _mark_registered(target)
    return target


def parametrize(
    parameters: Iterable[dict[str, Any]],
    *,
    name: str | None = None,
    ids: Sequence[str] | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Unpack[BenchmarkOptions],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a parametrized benchmark family.

    One registered benchmark per item in ``parameters``; each variant binds
    its kwargs into the wrapped function and exposes a ``[label]`` suffix on
    the registration name.

    Parameters
    ----------
    parameters : Iterable[dict[str, Any]]
        One dict of kwargs per variant. Snapshotted eagerly, so generators
        are fine.
    name : str, optional
        Override the auto-derived base name. Variant labels are still
        appended.
    ids : Sequence[str], optional
        Explicit labels (one per parameter dict). When omitted, labels are
        derived from the kwargs (e.g. ``n=10-algo=merge``).
    tags : Iterable[str] or str, optional
        Labels applied to every variant.
    **options
        Google Benchmark options applied to every variant. Same keys as
        :func:`benchmark`.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        Decorator that registers the family and returns the original
        function unchanged.

    Raises
    ------
    ValueError
        If ``ids`` is provided and its length doesn't match ``parameters``.
    TypeError
        If ``options`` contains an unknown key.
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

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        return _register_family(
            target,
            variants,
            name=name,
            ids=ids,
            options=options,
            tags=norm_tags,
        )

    return deco


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
    **iterables: Iterable[Any],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a benchmark family from the cartesian product of iterables.

    Each ``**iterables`` kwarg names a parameter and supplies its values; one
    benchmark is registered per tuple in the cartesian product.

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
    **iterables
        Parameter name → iterable of values. The cartesian product across
        these defines the registered variants.

    Returns
    -------
    Callable[[BenchmarkFn], BenchmarkFn]
        Decorator that registers the family and returns the original
        function unchanged.

    Raises
    ------
    TypeError
        If no iterables are supplied.
    RuntimeError
        If the same function is already registered via another decorator.

    Examples
    --------
    >>> @mew.product(n=[10, 100], algo=["merge", "quick"],
    ...              tags=("sort",), min_time=0.05)
    ... def bench_sort(state, n, algo):
    ...     ...

    Registers 4 benchmarks (one per ``(n, algo)`` pair).
    """
    if not iterables:
        raise TypeError("@product needs at least one iterable kwarg")

    options: BenchmarkOptions = {}
    if min_time is not None:
        options["min_time"] = min_time
    if min_warmup_time is not None:
        options["min_warmup_time"] = min_warmup_time
    if iterations is not None:
        options["iterations"] = iterations
    if repetitions is not None:
        options["repetitions"] = repetitions
    if unit is not None:
        options["unit"] = unit
    if use_real_time:
        options["use_real_time"] = True
    if use_manual_time:
        options["use_manual_time"] = True
    if measure_process_cpu_time:
        options["measure_process_cpu_time"] = True
    if report_aggregates_only:
        options["report_aggregates_only"] = True

    norm_tags = _normalize_tags(tags)
    keys = list(iterables.keys())
    value_lists = [list(v) for v in iterables.values()]
    variants = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*value_lists)]

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        return _register_family(
            target,
            variants,
            name=name,
            ids=ids,
            options=options,
            tags=norm_tags,
        )

    return deco
