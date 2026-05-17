"""Benchmarks with distinct CPU call patterns.

Designed so that `mew run benchmarks/bench_cpu.py --cpu-output cpu.html`
produces a report where each benchmark's branch is visually distinct:
- `bench_sort_builtin` is a single wide C-level frame in `sorted`.
- `bench_sort_quicksort` shows deep self-recursion into `_quicksort`.
- `bench_sort_bubble` shows a flat double-loop hot path.
- `bench_fib_naive` shows the exponential branching of naive recursion.
"""

import mew

_BIG = list(range(2000, 0, -1))
_SMALL = list(range(100, 0, -1))


def _quicksort(arr: list[int]) -> list[int]:
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return _quicksort(left) + middle + _quicksort(right)


def _bubble_sort(arr: list[int]) -> list[int]:
    arr = list(arr)
    n = len(arr)
    for i in range(n):
        for j in range(n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return arr


def _fib_naive(n: int) -> int:
    if n < 2:
        return n
    return _fib_naive(n - 1) + _fib_naive(n - 2)


@mew.benchmark(tags=("cpu",))
def bench_sort_builtin(state: mew.State) -> None:
    for _ in state:
        sorted(_BIG)


@mew.benchmark(tags=("cpu",))
def bench_sort_quicksort(state: mew.State) -> None:
    for _ in state:
        _quicksort(_BIG)


@mew.benchmark(tags=("cpu",))
def bench_sort_bubble(state: mew.State) -> None:
    for _ in state:
        _bubble_sort(_SMALL)


@mew.benchmark(tags=("cpu",))
def bench_fib_naive(state: mew.State) -> None:
    for _ in state:
        _fib_naive(15)
