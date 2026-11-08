"""Decorators that register benchmarks: ``@benchmark``, ``@parametrize`` and ``@product``."""

from __future__ import annotations

import inspect
import itertools
import os
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from functools import wraps
from pathlib import Path
from types import FrameType
from typing import Any, Unpack, overload

from mew._core import State, TimeUnit
from mew._options import normalize_options
from mew._registry import REGISTRY, Entry
from mew._typing import BenchmarkFn, BenchmarkOptions, TimeUnitStr


def _format_id_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return repr(value)
    return type(value).__name__


def _qualified_name(fn: BenchmarkFn, file: str | None) -> str:
    """Pytest-style nodeid: ``path/to/bench_foo.py::bench_name``.

    The path is relative to the file's project root, the nearest directory
    above it with a ``pyproject.toml``, so a benchmark keeps its name, which
    ``mew compare`` matches by, wherever mew runs from. Without a project, it
    is relative to the working directory. Falls back to module + qualname when
    the source file can't be resolved.
    """
    qualname = fn.__qualname__
    if not file:
        module = getattr(fn, "__module__", "<unknown>")
        return f"{module}::{qualname}"
    path = Path(file).resolve()
    root = _project_root(path.parent) or Path.cwd()
    try:
        # relpath, not relative_to: outside the root it gives `../x.py`, which
        # can't collide as a bare file name would. With `/` on every OS, so
        # results from any OS match in `mew compare`.
        rel = Path(os.path.relpath(path, root)).as_posix()
    except ValueError:  # on another drive than the root, on Windows
        rel = path.name
    return f"{rel}::{qualname}"


def _project_root(directory: Path) -> Path | None:
    """The nearest of ``directory`` and its parents that has a ``pyproject.toml``."""
    return next(
        (d for d in (directory, *directory.parents) if (d / "pyproject.toml").is_file()), None
    )


def body_frame(frame: FrameType) -> tuple[FrameType, State | None]:
    """The benchmark body's frame and its ``State``, from the frame that iterates the State.

    Profilers start from the body's first loop step, so ``frame`` is where the
    loop runs, which may be a helper the body calls. The body is the outermost
    frame holding the same ``State``, not counting mew's own family wrapper.
    Without a ``State`` in ``frame``'s locals, ``frame`` stands for the body.
    """
    state = next((v for v in frame.f_locals.values() if isinstance(v, State)), None)
    if state is None:
        return frame, None
    body, caller = frame, frame.f_back
    while caller is not None and any(v is state for v in caller.f_locals.values()):
        code = caller.f_code
        if not (code.co_name == "trampoline" and code.co_filename == __file__):
            body = caller
        caller = caller.f_back
    return body, state


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
    """Add ``target`` to the registry as ``fn``."""
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
        If an option is unknown or has the wrong type.
    ValueError
        If an option is out of range or contradicts another, ``name``
        contains ``::``, brackets or a newline, or a benchmark of that name is
        already registered, as when another decorator registered the function.

    Examples
    --------
    >>> @mew.benchmark
    ... def bench_sort(state):
    ...     for _ in state:
    ...         sorted([3, 1, 2])
    """
    norm_options = normalize_options(options)
    if name is not None:
        _check_addressable(name, "benchmark name")
    norm_tags = _normalize_tags(tags)

    def deco(target: BenchmarkFn) -> BenchmarkFn:
        return _register(target, target, name=name, options=norm_options, tags=norm_tags)

    if fn is not None:
        if not callable(fn):  # e.g. @mew.benchmark("name")
            raise TypeError(f"@benchmark takes its options as keywords, got {fn!r}")
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
    if ids is not None:
        # A string would split into one-character labels.
        if isinstance(ids, str) or not all(isinstance(i, str) for i in ids):
            raise TypeError(f"ids must be a sequence of strings, got {ids!r}")
        labels = list(ids)
    else:
        labels = ["-".join(f"{k}={_format_id_value(v)}" for k, v in kw.items()) for kw in cases]
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

    # The runner registers each case on its own, with its index bound as `case`.
    @wraps(target)
    def trampoline(state, case, _fn=target, _cases=cases, _labels=labels):
        # A default the body may override; the registered name carries the case.
        state.set_label(_labels[case])
        return _fn(state, **_cases[case])

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
        labels are equal, a name or label contains ``::``, brackets or a
        newline, an option is out of range or contradicts another, or a
        benchmark of that name is already registered.
    TypeError
        If an option is unknown or has the wrong type.

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
    norm_options = normalize_options(options)
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
    min_time: float | str | None = None,
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
    min_time : float or str, optional
        Seconds to measure each case (``0.5``, ``"500ms"``, ``"1m"``), or a
        fixed iteration count like ``"100x"``, the same as ``iterations=100``.
    min_warmup_time : float, optional
        Seconds to warm up each case before measuring it.
    iterations, repetitions : int, optional
        A fixed iteration count, which excludes the two times above, and how
        many runs per case.
    unit : str or TimeUnit, optional
        The time unit results are reported in: ``"ns"``, ``"us"``, ``"ms"`` or
        ``"s"``.
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
        If no iterables are given, or an option has the wrong type.
    ValueError
        If an option is out of range or contradicts another, two labels are
        equal, a name or label contains ``::``, brackets or a newline, or a
        benchmark of that name is already registered.

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
    options = normalize_options(
        {k: v for k, v in local_options.items() if v is not None and v is not False}
    )

    norm_tags = _normalize_tags(tags)
    keys = tuple(iterables)
    # itertools.product snapshots each axis, including generators.
    cases = [
        dict(zip(keys, combo, strict=True)) for combo in itertools.product(*iterables.values())
    ]
    return lambda target: _register_family(
        target, cases, name=name, ids=ids, options=options, tags=norm_tags
    )
