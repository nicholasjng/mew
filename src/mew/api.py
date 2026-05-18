"""Public decorators: @benchmark, @parametrize, @product."""

from __future__ import annotations

import functools
import inspect
import itertools
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, overload

from mew._registry import REGISTRY, Entry
from mew._typing import BenchmarkFn

_REGISTERED_ATTR = "__mew_registered__"

_OptionKeys = frozenset(
    {
        "min_time",
        "min_warmup_time",
        "iterations",
        "repetitions",
        "unit",
        "use_real_time",
        "use_manual_time",
        "measure_process_cpu_time",
        "report_aggregates_only",
    }
)


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


def _check_options(options: dict[str, Any]) -> None:
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


def _make_variant(
    fn: BenchmarkFn,
    kwargs: dict[str, Any],
    *,
    name: str,
    qualname: str,
) -> BenchmarkFn:
    """Wrap `fn` so it only takes a State; kwargs are bound as defaults."""

    @functools.wraps(fn, assigned=("__module__", "__doc__"))
    def variant(state, _fn=fn, _kw=kwargs):
        return _fn(state, **_kw)

    variant.__name__ = name
    variant.__qualname__ = qualname
    return variant


# ---------- @benchmark ------------------------------------------------------


@overload
def benchmark(fn: BenchmarkFn, /) -> BenchmarkFn: ...
@overload
def benchmark(
    *,
    name: str | None = None,
    tags: Iterable[str] | str | None = None,
    min_time: float | None = None,
    min_warmup_time: float | None = None,
    iterations: int | None = None,
    repetitions: int | None = None,
    unit: str | None = None,
    use_real_time: bool = False,
    use_manual_time: bool = False,
    measure_process_cpu_time: bool = False,
    report_aggregates_only: bool = False,
) -> Callable[[BenchmarkFn], BenchmarkFn]: ...


def benchmark(
    fn: BenchmarkFn | None = None,
    /,
    *,
    name: str | None = None,
    tags: Iterable[str] | str | None = None,
    **options: Any,
) -> Any:
    """Register a function as a single benchmark.

    Use `@parametrize` or `@product` for benchmark families. `tags` is an
    iterable of strings used by `mew run --tag <name>` for filtering.
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
                options=dict(options),
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
    options: dict[str, Any],
    tags: frozenset[str],
) -> BenchmarkFn:
    if ids is not None:
        ids = list(ids)
        if len(ids) != len(variants):
            raise ValueError(f"ids has {len(ids)} entries but parameters has {len(variants)}")

    file = _source_file(target)
    base = name or _qualified_name(target, file)
    module = getattr(target, "__module__", None)

    for i, kwargs in enumerate(variants):
        label = ids[i] if ids is not None else _default_id(kwargs)
        full = f"{base}[{label}]"
        variant = _make_variant(
            target,
            kwargs,
            name=f"{target.__name__}[{label}]",
            qualname=f"{target.__qualname__}[{label}]",
        )
        REGISTRY.add(
            Entry(
                name=full,
                fn=variant,
                module=module,
                file=file,
                options=dict(options),
                tags=tags,
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
    **options: Any,
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a parametrized benchmark family.

    Each item in `parameters` is a dict of kwargs passed to the wrapped function
    in addition to the State. One registered benchmark per dict. `tags` apply
    to every variant.

    Example::

        @mew.parametrize([
            {"n": 10, "algo": "merge"},
            {"n": 100, "algo": "quick"},
        ], min_time=0.05, tags=("sort",))
        def bench_sort(state, n, algo):
            data = list(range(n, 0, -1))
            for _ in state:
                sorted(data)
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
    unit: str | None = None,
    use_real_time: bool = False,
    use_manual_time: bool = False,
    measure_process_cpu_time: bool = False,
    report_aggregates_only: bool = False,
    **iterables: Iterable[Any],
) -> Callable[[BenchmarkFn], BenchmarkFn]:
    """Register a benchmark family from the cartesian product of `iterables`.

    Each `**iterables` kwarg names a parameter and supplies its values.
    Benchmark options (`min_time`, `unit`, ...) are explicit, typed kwargs.

    Example::

        @mew.product(n=[10, 100], algo=["merge", "quick"],
                     tags=("sort",), min_time=0.05)
        def bench_sort(state, n, algo):
            ...

    Registers 4 benchmarks (one per (n, algo) pair).
    """
    if not iterables:
        raise TypeError("@product needs at least one iterable kwarg")

    options: dict[str, Any] = {}
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
