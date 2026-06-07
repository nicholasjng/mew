"""Shared typing primitives for mew's public API."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from mew._core import TimeUnit

TimeUnitStr = Literal["ns", "us", "ms", "s"]


class BenchmarkOptions(TypedDict, total=False):
    """Per-benchmark Google Benchmark options accepted by the decorators.

    All keys are optional; omit a key to fall back to Google Benchmark's
    default. Used as ``**options: Unpack[BenchmarkOptions]`` in
    :func:`mew.benchmark` and :func:`mew.parametrize`.
    """

    min_time: float
    min_warmup_time: float
    iterations: int
    repetitions: int
    unit: TimeUnitStr | TimeUnit
    use_real_time: bool
    use_manual_time: bool
    measure_process_cpu_time: bool
    report_aggregates_only: bool


@runtime_checkable
class State(Protocol):
    """Structural state passed into benchmark targets.

    Matched by the C++ `_core.State` exposed via nanobind, and by `_MockState`
    used for out-of-loop profile passes (memory, CPU). Covers the surface
    benchmark bodies actually call: iteration, timing control, counters,
    labels, and range/thread accessors.
    """

    range_size: int
    threads: int
    thread_index: int
    name: str
    skipped: bool
    error_occurred: bool

    @property
    def iterations(self) -> int: ...
    @property
    def max_iterations(self) -> int: ...
    def __iter__(self) -> State: ...
    def __next__(self) -> None: ...
    def pause(self) -> AbstractContextManager[None]: ...
    def set_counter(self, name: str, value: float) -> None: ...
    def set_label(self, label: str) -> None: ...
    def set_items_processed(self, n: int) -> None: ...
    def set_bytes_processed(self, n: int) -> None: ...
    def set_iteration_time(self, seconds: float) -> None: ...
    def skip_with_error(self, msg: str) -> None: ...
    def skip_with_message(self, msg: str) -> None: ...
    def range(self, pos: int = 0) -> int: ...


@runtime_checkable
class BenchmarkFn(Protocol):
    """A callable benchmark target.

    Bound to Google Benchmark via the `@benchmark` / `@parametrize` / `@product`
    decorators. The first positional argument is a `State` (structural);
    parametrized families may take additional keyword arguments that get bound
    at variant construction time.

    Declaring `__name__` / `__qualname__` on the protocol lets the registration
    code read those attributes directly instead of going through `getattr` or
    narrowing to `types.FunctionType`.
    """

    __name__: str
    __qualname__: str

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...
