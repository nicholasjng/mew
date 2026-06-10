"""Iter dispatch overhead: `for _ in state` vs `state.batches`.

The body is a trivial integer add so per-iteration dispatch dominates.
Expected: the batched variant trims ~5 ns per iter (~25% faster on this body).
"""

import mew


@mew.benchmark(tags=("batched",))
def bench_naive(state: mew.State) -> None:
    a, b = 1, 2
    for _ in state:
        a + b


@mew.benchmark(tags=("batched",))
def bench_batched(state: mew.State) -> None:
    a, b = 1, 2
    for n in state.batches(1024):
        for _ in range(n):
            a + b
