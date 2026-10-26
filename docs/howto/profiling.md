# Profile CPU time and allocations

A profiler runs an extra, untimed pass of each benchmark repetition, so the timings stay clean.
The pass covers the loop, not the setup before it.
CPU sampling skips `state.pause()` blocks, while memory tracking continues through them.
Failed or skipped benchmarks get no profiling pass.
Use the timings to judge speed and the profile to find where the work happens.

## Sample CPU time

```console
$ mew run -k sort --cpu-profiler sample --profile-dir profiles
```

`--cpu-profiler sample` adds the sample count and hottest function to each row's `cpu_profile` block and to the terminal table.
`--profile-dir` writes the report to `profiles/cpu/index.html`.

| Sampler | Python 3.11–3.14 | Python 3.15+ | Free-threaded 3.15t |
| --- | --- | --- | --- |
| `sample` (built in) | pyinstrument | Tachyon | Tachyon |
| `mew.cpu:TachyonManager` | Unsupported | Tachyon | Tachyon |
| `mew.cpu:PyinstrumentManager` | pyinstrument | pyinstrument | Unsupported |

Tachyon writes a flame graph, which on free-threaded builds also lists each frame's bytecode instructions.
pyinstrument writes a call tree and needs the `cpu` extra.
It is rejected on free-threaded builds because it would enable the GIL.
Keep one sampler when comparing profiles across Python versions, since attribution differs.

If a pass collects no samples, raise `--min-time` or lower the sampling interval with a preset:

```toml
[tool.mew.profilers.fine]
class = "mew.cpu:TachyonManager"
interval = 1e-5
```

```console
$ mew run --cpu-profiler fine
```

A preset's `class` names the manager as `module:Class`, or as `path.py:Class` relative to `pyproject.toml`, and its other keys are keyword arguments.
A flag also takes a class spec directly, run with its defaults.
Every row records the preset, class, and options under `context.profilers`.

These samplers see Python frames.
Native work shows as a wide Python frame with nothing below it, and [native profiling](native-profiling.md) looks inside it.
On a GIL build, C calls that hold the GIL delay sampling, so time may land on the calling line or its enclosing loop.

## Track allocations

Install the `memory` extra on Linux or macOS.
This benchmark decodes a message created in setup:

```python
import json

import mew


@mew.benchmark
def bench_decode(state: mew.State) -> None:
    message = json.dumps({"values": list(range(100))})
    for _ in state:
        json.loads(message)
```

Record a baseline, make your change, and record a candidate:

```console
$ mew run --memory-profiler memray --repetitions 5 -o baseline.json
$ mew run --memory-profiler memray --repetitions 5 -o candidate.json
$ mew compare --metric memory.allocations_per_iteration baseline.json candidate.json
$ mew compare --metric memory.peak_bytes baseline.json candidate.json
```

The memory pass runs at most 16 iterations, or fewer if the timed run used fewer, and batched loops can exceed that cap.
It follows a timed run, so one-time costs such as first-call caches are already paid.
Compare `allocations_per_iteration`, since the total depends on the pass's iteration count.
`peak_bytes` is the tracked peak, not whole-process or device memory.
Threaded benchmarks are tracked on one thread, and allocations made directly by native code may be missing.

To find where the allocations come from, narrow the selection and write flame graphs:

```console
$ mew run -k bench_decode --memory-profiler memray --profile-dir profiles
```

`profiles/memory/` holds one flame graph per benchmark, and `index.html` links them.
Pass both `--cpu-profiler` and `--memory-profiler` to profile CPU and memory in one run.
After changing the code, repeat both the timing and the allocation comparison, since fewer allocations do not guarantee a faster workload.

## From Python

Import the benchmark modules, then pass managers to {func}`mew.run`:

```python
from pathlib import Path

import mew
from mew import cpu
from mew.memory import MemrayManager

profiler = cpu.manager()
mew.run(profiler_manager=profiler)
profiler.write_outputs(Path("profiles/cpu"))

tracker = MemrayManager()
mew.run(memory_manager=tracker)
tracker.write_outputs(Path("profiles/memory"))
```

A `MemrayManager` keeps its captures in a temporary directory it owns, so write its reports before it goes away.
See the [profiling reference](../reference/api/profiling.md) for the manager contract and writing your own.
