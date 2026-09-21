# Memory profiling

`--profile-memory` measures each benchmark with
[memray](https://bloomberg.github.io/memray/) in a separate, untimed pass.
Tracking covers the timing loop, not fixture setup.
The pass runs `min(--memory-iterations, iterations)` calls, 16 by default.

Use `allocations_per_iteration` for comparisons. `total_allocations` depends on
the memory-pass iteration count; `peak_bytes` is comparable as-is. Raise
`--memory-iterations` when a one-time allocation in the body skews the
per-iteration figure.
Memray results omit the optional cumulative `total_bytes` field because computing
it requires scanning every allocation in the capture.

## Prerequisites

```console
$ uv add 'mew-bench[memory]'    # or: pip install 'mew-bench[memory]'
```

Note: `memray` is not available on Windows.

## Basics

```console
$ mew run --profile-memory
mew · host=laptop cpus=10 …
Benchmark         │  Iters │ Real │ Peak Mem
────────────────────────────────────────────
bench_alloc_list  │ 50,000 │ 8 µs │  3.2 MB
```

Write a flame graph:

```console
$ mew run --flamegraph alloc.html
```

This implies `--profile-memory`. The self-contained HTML report combines the
captures from the selected benchmarks.

## Caveats

- Direct allocations by C extensions may not appear.
- Memory-pass iterations differ from the timing-table iteration count.
- Tracker overhead makes very small allocation measurements approximate.
