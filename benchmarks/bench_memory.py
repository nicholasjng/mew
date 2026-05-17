"""Benchmarks with intentionally varying memory footprints.

Three families (list, dict, bytearray) show distinct call-stack branches
in the flame graph, with allocation sizes scaling clearly across instances.
"""

import mew

_SIZES = [1_000, 10_000, 100_000, 500_000]


@mew.parametrize([{"n": n} for n in _SIZES], tags=("memory",))
def bench_list(state: mew.State, n: int) -> None:
    for _ in state:
        _ = list(range(n))


@mew.parametrize([{"n": n} for n in _SIZES], tags=("memory",))
def bench_dict(state: mew.State, n: int) -> None:
    for _ in state:
        _ = {i: i for i in range(n)}


@mew.parametrize([{"n": n} for n in _SIZES], tags=("memory",))
def bench_bytearray(state: mew.State, n: int) -> None:
    for _ in state:
        _ = bytearray(n)
