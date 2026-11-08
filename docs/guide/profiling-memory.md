# Memory profiling

`--memory-profiler memray` measures each benchmark with
[memray](https://bloomberg.github.io/memray/) in a separate, untimed pass.
Tracking covers the timing loop, not fixture setup.
The pass requests `min(--memory-iterations, iterations)` iterations, 16 by
default. Batched loops can exceed that budget; the memory result records the
actual iteration count. Like Google Benchmark, the pass runs a single thread,
also for threaded benchmarks, where `state.threads` reports 1. Peak memory of
a threaded benchmark therefore covers one thread's working set.

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

Note: `memray` is not available on Windows. Without memray, `mew run
--memory-profiler memray` stops with exit code 2 and the install hint, before any
benchmark runs; from Python, {func}`mew.memory.manager` raises `ImportError`.

## Basics

```console
$ mew run --memory-profiler memray
mew · cpu=Apple M1 Pro cpus=10 scaling=enabled
Benchmark            │        Iters │       Real │        CPU │   Peak Mem
──────────────────────────────────────────────────────────────────────────
bench_alloc_list     │       50,000 │    8.01 us │    7.99 us │     3.2 MB
```

Write a flame graph per benchmark:

```console
$ mew run --memory-profiler memray --profile-dir profiles/
```

Each benchmark gets a self-contained HTML report in `profiles/memory/`, numbered in run order and named after its function, such
as `0-bench_alloc_list.html`. `profiles/memory/index.html` links them all by
the benchmark's full name, case label included; in a report, memray's Stats
dialog shows it as the command line. A benchmark's first repetition stands for the others. The
graph's root under memray's `<tracker>` is the benchmark body, so what the body
allocates itself shows there.
A benchmark whose timed run fails or is skipped gets no memory pass, so it
contributes neither row metrics nor a flame graph.

## Caveats

- Direct allocations by C extensions may not appear.
- Memory-pass iterations differ from the timing-table iteration count.
- Tracker overhead makes very small allocation measurements approximate.
