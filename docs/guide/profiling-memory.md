# Memory profiling

`--profile-memory` runs each selected benchmark under [memray](https://bloomberg.github.io/memray/),
separately from the timing pass, and attaches peak memory, total memory usage, allocation count, and per-iteration allocation metadata to each run.

The capture runs a short warmup, then `--memory-iterations` measured loop passes (default 100), so one-time/first-call allocations (lazy init, connection setup) don't dominate the count.
The capture is scoped to the timing loop: tracking starts at the first `for _ in state` iteration and stops when the loop ends.
Fixture and setup allocations made before the loop are excluded, so the numbers describe the workload and stay comparable across suites with different setup strategies.

`total_allocations` is the cumulative count over all measured iterations, so it is **not** comparable across runs whose iteration counts differ.
For cross-engine / cross-run comparisons use `allocations_per_iteration` (`total_allocations / iterations`), the per-call figure; `peak_bytes` is a high-water mark and comparable as-is.

## Prerequisites

```console
$ uv add 'mew[memory]'    # or: pip install 'mew[memory]'
```

Note: `memray` is not available on Windows.

## Basics

```console
$ mew run --profile-memory
mew · host=laptop cpus=10 …
Benchmark         │  Iters │ Real │ Peak Mem │ Total Alloc
──────────────────────────────────────────────────────────
bench_alloc_list  │ 50,000 │ 8 µs │  3.2 MB  │   5.0 MB
```

Write a flame graph:

```console
$ mew run --flamegraph alloc.html
```

This implies `--profile-memory`, so the bare profile flag is optional.
The flame graph is a self-contained HTML page you can open directly in a browser.

## Caveats

- Memray captures Python-level allocations. C extensions allocating directly through `malloc` may or may not show up, depending on the extension.
- Like CPU profiling, the memory pass is **separate** from the timing pass, so the memory column won't line up with the iteration count from the timing column.
- Memray's tracker has measurable overhead. Treat allocations as approximate when they're already small.
- The loop-scoped capture applies to the stats columns. The `--flamegraph` capture wraps whole bodies (one tracker across all selected benchmarks), so setup allocations do appear there, useful when the fixture itself is the thing you're hunting.
