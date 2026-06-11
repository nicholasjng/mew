# Native profiling

`mew profile` profiles benchmarks **out of process**, so it captures native
(C/C++) stack frames — the ones a compiled extension spends its time in, which
the in-process `mew run --sample` ({doc}`profiling-cpu`) cannot see.

It launches a fresh worker process per benchmark case and lets a system profiler
sample the whole process while that case runs. The deliverable is an artifact you
open in the profiler's own viewer, not a column in the results table.

## Backends

`mew profile` selects a native-frame profiler with `--profiler` (default `auto`):

| Backend    | Platform        | Native frames | Artifact / viewer                  |
| ---------- | --------------- | ------------- | ---------------------------------- |
| `xctrace`  | macOS           | ✓             | `.trace` bundle → Instruments.app  |
| `py-spy`   | Linux, Windows  | ✓ (not macOS) | speedscope JSON / flamegraph       |
| `perf`     | Linux           | ✓             | `perf.data` → folded stacks        |

`auto` picks the platform's native backend. If none is available — for example
macOS with only the Command Line Tools and no full Xcode — it tells you so and
points you to `mew run --sample` for in-process Python sampling.

```{note}
`py-spy` and `perf` support is in progress; `xctrace` is the implemented backend
today. The others report their availability so `auto` selects correctly.
```

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
(navigate them with Instruments' run picker). Pass `--separate` for one
`<case>.trace` file each.

## Why a separate command

In-process samplers (pyinstrument, memray) run inside the benchmark interpreter
and return a small summary that `mew run` staples onto each timed row. A native
profiler must sample the process from the *outside*, so it can't enrich those
rows — it produces a trace file instead. Keeping it under `mew profile` makes the
trade-off explicit: `mew run --sample` for a quick Python-level summary in the
table, `mew profile` when you need to see into the C.

## Symbol resolution

Native frames are only readable if the extension carries debug symbols. Build it
`RelWithDebInfo` (or at least with `-g`) and keep the `.dSYM` (macOS) /
unstripped `.so` next to the module. A stripped release build shows
address-only frames in the native stack.

## Tuning

| Flag             | Default         | Notes                                                     |
| ---------------- | --------------- | --------------------------------------------------------- |
| `--iterations N` | `100000`        | Body reps under the sampler. Out-of-process samplers run at ~1 kHz, so fast benchmarks need many reps to accumulate stacks. |
| `--time-limit D` | none            | Hard cap per recording, e.g. `10s`. Bounds a runaway body. |

Like the in-process passes, profiling runs **separately** from timing — don't
read timings out of a profile.
