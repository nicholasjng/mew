# mew

[![Documentation Status](https://readthedocs.org/projects/mew/badge/?version=latest)](https://mew.readthedocs.io/en/latest/)
[![CI](https://github.com/nicholasjng/mew/actions/workflows/ci.yml/badge.svg)](https://github.com/nicholasjng/mew/actions/workflows/ci.yml)

A tool for microbenchmarking Python code, powered by [Google Benchmark](https://github.com/google/benchmark).

```python
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

```console
$ mew run
mew · host=laptop cpus=10 @ 3200MHz scaling=enabled
Benchmark                              │     Iters │       Real │        CPU
────────────────────────────────────────────────────────────────────────────
benchmarks/bench_sort.py::bench_sorted │ 1,000,000 │   32.10 ns │   32.05 ns
```

- **Decorate**: `@mew.benchmark`, `@mew.parametrize`, `@mew.product` register one benchmark or a benchmark family.
- **Run**: `mew run` discovers `bench_*.py` files, streams results to a formatted table, JSON, or a JSONL archive.
- **Profiling**: `mew run --sample` (in-process pyinstrument) and `--profile-memory` (memray allocations) in the same run; `mew profile` for native C frames via xctrace / py-spy / perf.
- **Compare**: `mew compare baseline.json head.json --fail-on-regression 5` for CI jobs.

## Installation

With a `[tool.uv.sources]` entry in your `pyproject.toml`:

```toml
[tool.uv.sources]
mew = { git = "https://github.com/nicholasjng/mew" }
```

```console
$ uv add mew                 # or: pip install mew
$ uv add 'mew[cpu,memory]'   # opt-in profilers
```

Requires Python 3.11+. A pre-built wheel is installed where available;
otherwise the C++ extension is compiled via CMake + nanobind.

A PyPI release is planned for the future.

## Docs

- Quickstart, concepts, user guide: <https://mew.readthedocs.io/>
- API reference: <https://mew.readthedocs.io/en/latest/reference/api/>
- CLI reference: <https://mew.readthedocs.io/en/latest/reference/cli.html>

## License

This project is licensed under the Apache-2.0 license.
