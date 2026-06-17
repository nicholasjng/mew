"""Free-threading scaling: independent work scales, contended work doesn't.

**Free-threaded only.** Both benchmarks set ``thread_range``, so ``mew run``
refuses them on a stock (GIL) interpreter — run on a ``python3.13t`` build:

    $ uv run --python 3.13t mew run benchmarks/bench_threads.py --tag ft

Each runs the *same* CPU-bound kernel at 1, 2, 4, 8 threads. They differ only in
whether the kernel touches shared state:

- ``bench_independent`` — each thread spins on its own local buffer. No sharing,
  so on a free-threaded interpreter the threads run on separate cores: wall-clock
  (``Real``) stays roughly flat and ``items_per_second`` scales up with the
  available cores, then plateaus once they're saturated (e.g. ~4x on a 4
  performance-core machine, with little further gain on efficiency cores). ``Real
  ≈ CPU`` throughout — the work really is running in parallel. Under a GIL none
  of this could happen.
- ``bench_contended`` — the *identical* kernel, but under one global lock. The
  lock serializes the work, so ``Real`` climbs ~linearly with the thread count
  while ``CPU`` (the actual work per iteration) stays flat — the ``Real >> CPU``
  gap *is* the time threads spend blocked on the lock. ``items_per_second``
  barely moves. This is what "free-threading didn't help" looks like, and why the
  lock is the thing to measure.

Both use ``use_real_time=True`` so the reported figure is wall-clock (the metric
that drops under real parallelism), not summed-across-threads CPU.
"""

import threading

import mew

_LOCK = threading.Lock()
_SIZE = 256  # byte buffer; indices and values stay in the immortal small-int range.
_PASSES = 4000  # in-place transforms over the buffer per timed iteration.
_WORK = _SIZE * _PASSES  # byte-ops per iteration, per thread.


def _spin() -> None:
    """CPU busy-loop with no shared state and ~no allocation.

    The buffer is local (one per call) and the inner loop only touches bytes and
    indices in ``0..255`` — all immortal small ints — so it neither allocates nor
    refcounts in the hot path. That keeps the allocator out of the picture, so
    what scales (or doesn't) is the actual CPU work, not malloc contention.
    """
    buf = bytearray(_SIZE)
    for _ in range(_PASSES):
        for i in range(_SIZE):
            buf[i] = (buf[i] * 31 + 7) & 0xFF


@mew.benchmark(thread_range=(1, 8), use_real_time=True, tags=("ft", "scaling"))
def bench_independent(state: mew.State) -> None:
    for _ in state:
        _spin()
    # Summed across threads by GB → items_per_second is aggregate throughput.
    state.set_items_processed(state.iterations * _WORK)


@mew.benchmark(thread_range=(1, 8), use_real_time=True, tags=("ft", "contended"))
def bench_contended(state: mew.State) -> None:
    for _ in state:
        with _LOCK:
            _spin()
    state.set_items_processed(state.iterations * _WORK)
