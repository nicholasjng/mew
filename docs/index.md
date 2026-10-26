# mew

mew measures the cost of Python code using [Google Benchmark](https://github.com/google/benchmark).
Write a function, put the work you want to measure inside its loop, and run it from Python or the command line.

```python
import mew


@mew.benchmark
def bench_sorted(state: mew.State) -> None:
    data = list(range(1000, 0, -1))
    for _ in state:
        sorted(data)
```

Save this as `benchmarks/bench_sort.py` and run `mew run`.
mew chooses an iteration count and reports the time per iteration.

## Where to start

- [Quickstart](quickstart.md): install mew, write a benchmark, and compare two runs.
- [Writing and measuring benchmarks](guide/writing-benchmarks.md): measure the work you intended and know when to trust the result.
- [Comparing and gating](guide/comparing.md): compare result files and fail CI on regressions.
- [Using mew from Python](guide/python-api.md): select, run, and read results in your own scripts.
- [How-tos](howto/profiling.md) for profiling and comparing implementations, and [recipes](recipes/index.md) for running mew in production.

```{toctree}
:caption: Guide
:maxdepth: 1

quickstart
guide/writing-benchmarks
guide/comparing
guide/python-api
```

```{toctree}
:caption: How-tos
:maxdepth: 1

howto/profiling
howto/ab-comparison
howto/native-profiling
```

```{toctree}
:caption: Recipes
:maxdepth: 1

recipes/index
recipes/ci-gate
recipes/git-history
recipes/analyzing-results
```

```{toctree}
:caption: Reference
:maxdepth: 1

reference/api/index
reference/cli
reference/configuration
reference/results
```

```{toctree}
:caption: Development
:maxdepth: 1

development/building
development/sanitizers
development/patches
development/contributing
development/releasing
changelog
```
