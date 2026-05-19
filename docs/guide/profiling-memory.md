# Memory profiling

`--profile-memory` runs each selected benchmark under [memray](https://bloomberg.github.io/memray/) once,
separately from the timing pass, and attaches peak memory, total memory usage, and allocation count metadata to each run.

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
The resulting flame graph is a self-contained HTML page, and can be inspected directly in the browser.

## Caveats

- Memray captures Python-level allocations. Allocations made by C extensions through `malloc` directly may or may not show up depending on the extension.
- Like CPU profiling, the memory pass is **separate** from the timing pass.
Don't expect the memory column to line up with the iteration count from the timing column.
- Memray's tracker has measurable overhead. Treat allocations as approximate when they're already small.
