"""Shared typing primitives for mew's public API."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

from mew._core import TimeUnit

TimeUnitStr = Literal["ns", "us", "ms", "s"]


class BenchmarkOptions(TypedDict, total=False):
    """Per-benchmark Google Benchmark options accepted by the decorators.

    All keys are optional.
    Omit a key to fall back to Google Benchmark's default.
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

    Matched by the C++ ``_core.State`` and the ``_MockState`` used for out-of-loop profile passes.
    Covers iteration, timing control, counters, labels, and range/thread accessors.
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

    Bound to Google Benchmark via the ``@benchmark`` / ``@parametrize`` / ``@product`` decorators.
    The first positional argument is a :class:`State`; parametrized families bind additional kwargs at variant construction time.
    """

    __name__: str
    __qualname__: str

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...
