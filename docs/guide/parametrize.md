# Parametrizations

`@mew.parametrize` registers a benchmark _family_: one body, many variants.

## `@mew.parametrize`

Takes an iterable of keyword argument dicts, registering one variant per dict:

```python
@mew.parametrize(
    [
        {"n": 10, "algo": "merge"},
        {"n": 100, "algo": "quick"},
    ],
    min_time=0.05,
    tags="sort",
)
def bench_sort(state: mew.State, n: int, algo: str) -> None:
    data = list(range(n, 0, -1))
    for _ in state:
        sorted(data)
```

Registered names:

```
benchmarks/bench_sort.py::bench_sort[n=10-algo=merge]
benchmarks/bench_sort.py::bench_sort[n=100-algo=quick]
```

You can override the labels with the `ids` argument:

```python
@mew.parametrize(
    [{"n": 10}, {"n": 1000}],
    ids=["small", "large"],
)
def bench(state, n): ...
```

The length of `ids` **must** equal the number of parametrizations.

## Combinations with `mew.grid`

`mew.grid` makes one case per combination of independent axes, for
`@parametrize` to register:

```python
@mew.parametrize(mew.grid(n=[10, 100, 1000], algo=["merge", "quick"]), tags="sort")
def bench_sort(state, n, algo): ...
```

This registers six benchmarks, the last axis varying fastest:
`bench_sort[n=10-algo=merge]`, `bench_sort[n=10-algo=quick]`, and so on. Any
name can be an axis, including those of options such as `threads`, since
options go to `@parametrize` and axes to `grid`.

A grid is a plain list of dicts, so when only some combinations make sense,
join grids with `+` or filter one:

```python
# Every size with merge sort, but quick sort only on the small inputs
cases = mew.grid(n=[10, 100, 1000], algo=["merge"]) + mew.grid(n=[10, 100], algo=["quick"])

# The same, as the full grid minus what doesn't make sense
cases = [
    c for c in mew.grid(n=[10, 100, 1000], algo=["merge", "quick"])
    if not (c["algo"] == "quick" and c["n"] == 1000)
]
```

Grids that overlap give two cases the same label, which `@parametrize` rejects.

## Filtering at run time

Filter by variant label via the global `-k` pattern (substring match):

```console
$ mew run -k 'algo=quick'
$ mew run -k 'n=1000'
```

Or combine discovery and filtering with the selector form:

```console
$ mew run 'benchmarks/bench_sort.py::n=1000'
```

To see the individual cases a family expands to, list them by label:

```console
$ mew list --show-cases
benchmarks/bench_sort.py::bench_sort[n=100]
benchmarks/bench_sort.py::bench_sort[n=1000]
```

`-k` is a regex, so selecting one case by its displayed `name[label]` would mean
escaping the brackets (`bench_sort\[n=1000\]`). Pass `-F` / `--literal` to match
the pattern as a literal string and paste the name as shown:

```console
$ mew run -F -k 'bench_sort[n=1000]'
```
