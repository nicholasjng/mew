# Native profiling

`mew profile` profiles benchmarks **out of process**, so it captures native
(C/C++) stack frames — the ones a compiled extension spends its time in, which
the in-process `mew run --sample` ({doc}`profiling-cpu`) cannot see.

It launches a fresh worker process per benchmark case and lets a system profiler
sample the whole process while that case runs. The result is an artifact you
open in the profiler's own viewer, not a column in the results table.

## Backends

`mew profile` selects a native-frame profiler with `--profiler` (default `auto`):

| Backend    | Platform        | Native frames | Artifact / viewer                  |
| ---------- | --------------- | ------------- | ---------------------------------- |
| `xctrace`  | macOS           | ✓             | `.trace` bundle → Instruments.app  |
| `py-spy`   | Linux, Windows  | ✓ (not macOS) | speedscope JSON → speedscope.app   |
| `perf`     | Linux           | ✓             | `perf script` text → speedscope.app |

`auto` picks the platform's native backend. If none is available — for example
macOS with only the Command Line Tools and no full Xcode — it tells you so and
points you to `mew run --sample` for in-process Python sampling.

The `py-spy` and `perf` backends both emit a [speedscope](https://www.speedscope.app/)-readable
artifact, so one viewer covers both. `py-spy` needs `uv pip install py-spy` and,
in containers, `CAP_SYS_PTRACE`. `perf` is a system package whose version must
match the running kernel, and recording usually needs `kernel.perf_event_paranoid` lowered.

## Basics (xctrace / macOS)

`xctrace` ships with the full Xcode (not the Command Line Tools alone). If you see
an "xctrace needs the full Xcode" error, select it:

```console
$ sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```

Then record and open in Instruments:

```console
$ mew profile --open
$ mew profile -k bench_sort --open      # filter benchmarks like `mew run`
```

By default all cases land in one `mew.trace` bundle with one run per case
(navigate with Instruments' run picker). Pass `--separate` for one
`<case>.trace` file each.

### Templates

`--template` chooses which Instruments instruments record and how they're
configured; `mew profile` defaults to `Time Profiler` (CPU sampling). Pass any
other template name to record something else:

```console
$ xctrace list templates              # authoritative list for your Xcode version
$ mew profile --template Allocations
$ mew profile --template "System Trace"   # quote names with spaces
```

The exact set is Xcode-version-dependent, but the standard templates relevant to
mew are:

| Template        | Records                                                    |
| --------------- | --------------------------------------------------------- |
| `Time Profiler` | CPU call-stack sampling (the default).                    |
| `Allocations`   | Heap allocations over time, incl. C-extension `malloc`.   |
| `Leaks`         | Allocations plus periodic leak detection.                 |
| `System Trace`  | Syscalls, VM faults, thread scheduling — off-CPU insight.  |
| `CPU Counters`  | Hardware performance counters (instructions, cache misses). |

`--template` also accepts a **path to a `.tracetemplate`**, so a custom template
works with no change to mew:

```console
$ mew profile --template ~/Library/Application\ Support/Instruments/Templates/My.tracetemplate
```

**Do you need a custom one?** Almost never — the built-ins cover the axes mew
cares about, and `xctrace record` can only *use* a template, not author one. Make
your own only for a specific *combination* of instruments (e.g. Time Profiler +
Allocations in one recording) or non-default settings (e.g. a higher sampling
rate), built in the Instruments GUI via **File → Save As Template**, which writes
a `.tracetemplate` into `~/Library/Application Support/Instruments/Templates/`.
Pass that path to `--template`; pass-through already covers this, so there's no
CLI authoring path.

### Native memory and leaks

`--template Allocations` / `Leaks` turn `mew profile` into a native memory tool,
catching C-extension `malloc` that `mew run --profile-memory` (memray, Python-level)
misses. For a quick leak check without Instruments, the base-system `/usr/bin/leaks`
— available with just the Command Line Tools, *unlike* xctrace — inspects a process
for unreferenced blocks; run the target with `MallocStackLogging=1` for allocation
backtraces (`atos` symbolicates them). Caveat: a single snapshot conflates one-time
setup (lazy imports, interned strings, allocator arenas) with real leaks, so treat
the count as a starting point.

## Basics (py-spy / perf, Linux)

Both write one artifact per case into `--output-dir` (default `./.mew-traces`)
and load into [speedscope.app](https://www.speedscope.app/):

```console
$ mew profile -p py-spy            # *.speedscope.json per case
$ mew profile -p perf              # *.perf.txt per case (raw *.data kept alongside)
$ mew profile -p py-spy --open     # opens via the `speedscope` CLI if installed
```

If `npx speedscope` / the `speedscope` CLI isn't installed, `--open` prints the
artifact path to drag onto speedscope.app instead.

## Why a separate command

In-process samplers (pyinstrument, memray) run inside the benchmark interpreter
and return a small summary that `mew run` staples onto each timed row. A native
profiler samples the process from the *outside*, so it can't enrich those
rows — it produces a trace file instead. Keeping it under `mew profile` makes the
trade-off explicit: `mew run --sample` for a quick Python-level summary in the
table, `mew profile` when you need to see into the C.

## Symbol resolution

Native frames are readable only if the extension carries debug symbols. Build it
`RelWithDebInfo` (or at least with `-g`) and keep the `.dSYM` (macOS) /
unstripped `.so` next to the module. A stripped release build shows
address-only frames in the native stack.

## Tuning

| Flag             | Default         | Notes                                                     |
| ---------------- | --------------- | --------------------------------------------------------- |
| `--iterations N` | `100000`        | Body reps under the sampler. Out-of-process samplers run at ~1 kHz, so fast benchmarks need many reps to accumulate stacks. |
| `--rate N`       | `1000`          | (py-spy/perf) Sampling frequency in Hz. Ignored by xctrace. |
| `--time-limit D` | none            | Hard cap per recording, e.g. `10s`. Bounds a runaway body. |

Like the in-process passes, profiling runs **separately** from timing — don't
read timings out of a profile.

## What native profiles include (and don't)

Two differences from `mew run --sample` when reading a native profile:

- **`state.pause()` regions are *not* excluded.** The in-process sampler
  suspends sampling for the duration of a `pause()` block, so setup excluded from
  timing is also excluded from the profile. A native profiler samples the whole
  process from the outside and has no hook into `pause()`, so **everything in the
  benchmark body — setup outside the `for _ in state` loop and paused regions
  included — shows up in the samples.** If a hot frame in the flame graph is
  really one-time setup, that's why. Move setup out of the profiled body, or read
  the profile knowing it's there.

- **On-CPU only.** Sampling profilers count where the program is *running*, not
  where it's *blocked* — time spent waiting on the GIL, locks, or I/O is largely
  invisible. If a benchmark looks fast on-CPU but slow on the clock, the gap is
  off-CPU time. `py-spy --idle` surfaces some of it; perf can approximate it via
  scheduler tracepoints. Neither is wired up by default.
