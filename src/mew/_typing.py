"""Shared typing primitives for mew's public API."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any, Literal, NotRequired, Protocol, TypedDict, runtime_checkable

from mew._core import TimeUnit

TimeUnitStr = Literal["ns", "us", "ms", "s"]


class RunRow(TypedDict):
    """One benchmark run, serialized: the contract every reporter consumes.

    Reporters read these dicts, never the live C++ :class:`~mew._core.Run`. The
    base keys (``name``, ``real_time``, ``cpu_time``, ``iterations``,
    ``time_unit``, ``label``, ``counters``, …) are always present; the keys below
    are optional overlays.

    Attributes
    ----------
    variant : str, optional
        Variant name, set by the ``--variant`` orchestrator.
    custom : dict, optional
        Per-suite :func:`mew.set_context` values.
    memory : dict, optional
        :func:`dataclasses.asdict` of a :class:`~mew.memory.MemoryProfile`
        (present under ``--profile-memory``).
    cpu_profile : dict, optional
        :func:`dataclasses.asdict` of a :class:`~mew.cpu.CPUProfile`
        (present under ``--sample``).
    """

    name: str
    run_name: str
    family_index: int
    per_family_instance_index: int
    run_type: str
    aggregate_name: str
    repetitions: int
    repetition_index: int
    threads: int
    iterations: int
    real_time: float
    cpu_time: float
    real_accumulated_time: float
    cpu_accumulated_time: float
    time_unit: str
    label: str
    skipped: bool
    skip_message: str
    counters: dict[str, float]
    variant: NotRequired[str]
    custom: NotRequired[dict[str, Any]]
    memory: NotRequired[dict[str, Any]]
    cpu_profile: NotRequired[dict[str, Any]]


class BenchmarkOptions(TypedDict, total=False):
    """Per-benchmark Google Benchmark options accepted by the decorators.

    All keys are optional; omit one to fall back to Google Benchmark's default.

    ``threads`` and ``thread_range`` enable Google Benchmark's threaded mode (each
    thread gets its own ``State`` and timer). They **require** a free-threaded
    interpreter: under the GIL the trampoline holds the GIL across
    Google Benchmark's per-thread start barrier, so the workers deadlock rather
    than run. On a GIL build :func:`mew.run` warns and skips threaded benchmarks
    by default (``strict=True`` raises instead). ``thread_range`` runs once per
    thread count in ``[min, max]`` (powers of two) and is mutually exclusive with
    ``threads``.
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
    threads: int
    thread_range: tuple[int, int]


@runtime_checkable
class State(Protocol):
    """Structural state passed into benchmark targets.

    Matched by the C++ ``_core.State`` and the ``_ProfileState`` used for out-of-loop
    profile passes. Covers iteration, timing, counters, labels, and range/thread accessors.
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
    def keep_running_batch(self, n: int) -> bool: ...
    def batches(self, n: int) -> Iterator[int]: ...
    def pause(self) -> AbstractContextManager[None]: ...
    def set_counter(self, name: str, value: float, flags: int = ...) -> None: ...
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

    Bound to Google Benchmark via the ``@benchmark`` / ``@parametrize`` / ``@product``
    decorators. The first positional argument is a :class:`State`; parametrized families
    bind additional kwargs at variant construction time.
    """

    __name__: str
    __qualname__: str
    __globals__: dict[str, Any]

    def __call__(self, state: State, /, *args: Any, **kwargs: Any) -> None: ...
