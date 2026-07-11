# Native profiling (recipe)

mew's in-process profilers ({doc}`profiling-cpu`, {doc}`profiling-memory`) see
Python frames. To see **native** (C/C++) frames — where a compiled extension
actually spends its time — sample the whole process from the outside with a
system profiler.

mew has no command for this on purpose: `py-spy`, `perf`, and `xctrace` are
better at it than a wrapper would be, and they already know how to write
artifacts their own viewers understand. What mew provides is the piece they
need: a way to run one benchmark body, on its own, in a fresh process.

## The runner script

Save this as `profile_one.py`. It imports a benchmark file, finds one entry, and
runs its body directly — no timing loop, no Google Benchmark.

```python
"""Run one benchmark body so an external profiler can sample it.

Usage: python profile_one.py <file.py> <benchmark-name> [iterations]
"""

import sys
from pathlib import Path

from mew import _discovery
from mew._registry import REGISTRY


class State:
    """Minimal stand-in for mew's State: iterate N times, no timing."""

    range_size = 0
    threads = 1
    thread_index = 0
    name = ""
    skipped = False
    error_occurred = False

    def __init__(self, n: int = 100_000, case: int = 0) -> None:
        self._n, self._i, self._case = n, 0, case

    @property
    def iterations(self) -> int:
        return self._n

    max_iterations = iterations

    def __iter__(self):
        return self

    def __next__(self) -> None:
        if self._i >= self._n:
            raise StopIteration
        self._i += 1

    def range(self, pos: int = 0) -> int:
        return self._case

    def pause(self):
        import contextlib

        return contextlib.nullcontext()

    def __getattr__(self, _name):  # set_counter, set_label, ... are all no-ops
        return lambda *a, **kw: None


def main() -> int:
    file, name, *rest = sys.argv[1:]
    iterations = int(rest[0]) if rest else 100_000
    REGISTRY.clear()
    _discovery.import_file(Path(file))
    entry = next((e for e in REGISTRY.all() if e.name.endswith(name)), None)
    if entry is None:
        print(f"no benchmark matching {name!r}", file=sys.stderr)
        return 1
    entry.fn(State(iterations))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Find the name to pass with `mew list -n`.

For a `@parametrize` family, pass the case index to `State(iterations, case=i)` —
that is what `state.range(0)` returns, which is how the family trampoline picks
its variant.

## Recording

**py-spy** (Linux, Windows; not macOS native frames) — needs
`uv pip install py-spy`, and `CAP_SYS_PTRACE` in containers:

```console
$ py-spy record --native --format speedscope -o profile.json -- \
      python profile_one.py benchmarks/bench_sort.py bench_sort
```

Open `profile.json` at [speedscope.app](https://www.speedscope.app/).

**perf** (Linux) — a system package whose version must match the kernel;
recording usually needs `kernel.perf_event_paranoid` lowered:

```console
$ perf record -F 1000 -g -- python profile_one.py benchmarks/bench_sort.py bench_sort
$ perf script > perf.txt
```

`perf.txt` also opens in speedscope.

**xctrace** (macOS) — needs full Xcode, not just the Command Line Tools:

```console
$ xctrace record --template "Time Profiler" --output bench.trace \
      --launch -- python profile_one.py benchmarks/bench_sort.py bench_sort
$ open bench.trace
```

Instruments reads the `.trace` bundle directly. `xctrace` will not emit
speedscope or pprof; if you need those formats, `xctrace export --xpath
'//trace-toc/run/data/table[@schema="time-profile"]'` dumps samples as XML that
you can fold into stacks yourself — but Instruments' own viewer is usually the
faster path.

## Choosing an iteration count

The script's default is 100,000. Aim for a few seconds of wall time: too few
iterations and the sampler collects nothing, too many and the recording is
unwieldy. Time one run first (`mew run -k bench_sort`) and divide.
