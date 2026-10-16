# CPU profiling

`--sample` profiles each benchmark in a separate pass and attaches the sample
count and hottest frame to its result, and on free-threaded 3.15+ the hottest
bytecode instruction. It samples with
[Tachyon](https://docs.python.org/3.15/library/profiling.sampling.html), the
standard library's sampling profiler, on Python 3.15+, and with
[pyinstrument](https://pyinstrument.readthedocs.io/) before. `--sampler` picks
one explicitly; see [Choosing a sampler](#choosing-a-sampler).

This is **in-process** sampling: it sees Python frames only. To capture native
(C/C++) frames from a compiled extension, see {doc}`profiling-native`.

## Prerequisites

Tachyon ships with Python 3.15+. pyinstrument, the default before 3.15, comes
with the `cpu` extra:

```console
$ uv add 'mew-bench[cpu]'    # or: pip install 'mew-bench[cpu]'
```

On free-threaded Python, `--sample` needs 3.15t and Tachyon. Pyinstrument's
native sampler would enable the GIL, so mew rejects pyinstrument on
free-threaded builds rather than letting the import change what is measured.

## Choosing a sampler

| `--sampler`      | Python 3.11–3.14 | Python 3.15+ | Free-threaded 3.15t |
| ---------------- | ---------------- | ------------ | ------------------- |
| `auto` (default) | pyinstrument     | Tachyon      | Tachyon             |
| `tachyon`        | rejected         | Tachyon      | Tachyon             |
| `pyinstrument`   | pyinstrument     | pyinstrument | rejected            |

The samplers attach the same `cpu_profile` keys (see [Row schema](#row-schema)),
but not always the same figures. pyinstrument records a sample only when a
function is called or returns, and hooks every call to do so; Tachyon samples
from outside the body. They agree on bodies that spend their time in loops.
On call-heavy bodies and on Python callbacks from C, the hottest function's
share can differ by up to 30 points, or the samplers name different functions.
There, pyinstrument's pass can take 4 to 8 times as long as the timed run;
Tachyon's takes 1.1 to 1.3 times as long.

So to compare CPU profiles of a suite across interpreter versions, sample every
version with one sampler: with `--sampler pyinstrument` on 3.15+, differences
between versions come from the code and the interpreter, not from the sampler.
Otherwise keep `auto`: Tachyon also names the hottest line, and on
free-threaded builds the hottest instructions. The `profiler` key and
`context.python` tell the rows apart.

## Basics

```console
$ mew run --sample
mew · host=laptop cpus=10 scaling=enabled
Benchmark            │        Iters │       Real │        CPU
─────────────────────────────────────────────────────────────
bench_sort_quick     │      100,000 │   24.10 us │   24.05 us
  12,341 samples · hottest _quicksort (bench.py:18)
```

Write a self-contained HTML report:

```console
$ mew run --sample --sample-html cpu.html
```

This implies `--sample`, so you can drop it:

```console
$ mew run --sample-html cpu.html
```

## Tuning the sampler

| Flag                  | Default | Notes                                     |
| --------------------- | ------- | ----------------------------------------- |
| `--sample-interval F` | `1e-4`  | Smaller = more samples = higher overhead. |
| `--sample-top-k K`    | `5`     | Instructions per row; see below. pyinstrument ignores it. |

The profiling pass is **separate** from the timing pass, so profiler overhead doesn't pollute timing numbers.
It repeats the iteration count selected by the timing pass; don't read timings out
of the profiling report.

Tachyon samples from a background thread that reads the benchmark thread's stack
out of the process's memory, so the body runs without instrumentation. On
free-threaded builds the sampler runs beside the body. On GIL builds each sample
needs the GIL, so for the duration of the pass mew lowers the interpreter's
switch interval to the sampling interval. The body still hands over the GIL only
at function calls and loop jumps. A C call that holds the GIL, like `sorted()` or
`json.dumps()`, therefore holds off the sampler until it returns, and samples
arrive below the requested rate. To keep such calls from vanishing from the
profile, mew counts each sample as the time since the previous one. Time is
still charged to wherever the body next handed over the GIL: usually the line
that made the call, sometimes the last line of the enclosing loop. A Python
function that C code calls back, like a `key=` function of `sorted()`, hands
over the GIL each time it starts. It can therefore collect the C code's time
between calls, or, if the samples fall elsewhere, go unsampled. On
free-threaded builds, samples name the line that was running.

## Reading the report

With Tachyon, `cpu.html` is a flame graph with one root per benchmark: stacks
start at the benchmark body, without mew's own call chain above it, and widths
are time. The hottest frame names the function with the most time of its own,
at its line with the most time. With pyinstrument, `cpu.html` is a call tree per
benchmark, and the hottest frame names no line: pyinstrument sees a line only
where a call or return happened, which for a loop is its `return`.

`<native>` frames mark where C code calls back into Python, like a `key=`
function that `sorted()` calls, or an `__eq__` that a dict lookup calls. Time
spent in C itself stays on the Python line that made the call.

On free-threaded builds, hovering a frame also lists the bytecode instructions it
was running, with specialized forms such as `BINARY_OP_ADD_INT` marked. A hot
instruction that stays generic is often the finding a microbenchmark is after:
`total += x` stays `BINARY_OP`, for example, once `total` is too large for the
small-int fast path. GIL builds leave the list out. There, a sample can only land
where the body hands over the GIL, so it would always name a call or a loop jump.

A benchmark whose timed run fails or is skipped gets no profiling pass, so it
appears in neither the row summaries nor the HTML report.

Because sampling sees Python frames only, a body that bottoms out in C — `sorted`,
a NumPy call, a compiled extension — collapses into one wide frame with no
detail beneath it. That flat frame is the signal to switch to
{doc}`profiling-native`, which can see inside it.

## Hottest instruction

On free-threaded 3.15+, each row also names the bytecode instruction that took
the most time, last on the line below it:

```console
$ mew run --sample
Benchmark            │        Iters │       Real │        CPU
─────────────────────────────────────────────────────────────
bench_small_ints     │      100,000 │   15.60 us │   15.58 us
  7,793 samples · hottest bench_small_ints (bench.py:18) · BINARY_OP_ADD_INT 48%
bench_big_ints       │       20,000 │   61.20 us │   61.15 us
  8,145 samples · hottest bench_big_ints (bench.py:25) · BINARY_OP 61%, 61% generic
```

Both loops run `total += i`. In the second, `total` is too large for the
small-int fast path, so the addition stays generic. The first percentage is the
instruction's share of the body's time. `generic` is the share of the body's
time spent in any instruction the interpreter can specialize but ran
unspecialized; the console leaves it out when it rounds to 0%.

JSON rows carry the `--sample-top-k` hottest instructions (default 5) in
`cpu_profile.top_instructions`, hottest first, plus `unspecialized_share`; see
[Row schema](#row-schema). `source` is the code the instruction covers, cut to
its first line and 40 characters. `unspecialized` marks a generic instruction
the interpreter can specialize. DuckDB reads the list as a list of structs:

```sql
SELECT benchmark, i.instruction, i.share
FROM read_json_auto('results.jsonl'), unnest(cpu_profile.top_instructions) AS t(i)
WHERE i.unspecialized;
```

On GIL builds and under pyinstrument, `top_instructions` is empty and
`unspecialized_share` is `null`, for the reason given under
[Reading the report](#reading-the-report). The query above then finds no rows
rather than failing.

A site that sees several types is not generic: the interpreter re-specializes
it. An `o.x` over objects of two classes in turn, for example, shows up as
`LOAD_ATTR_INSTANCE_VALUE` in some samples and `LOAD_ATTR_SLOT` in others. The
flame graph's per-frame instruction list shows both forms.

## Row schema

Each row's `cpu_profile` has the same keys and types whichever sampler and
interpreter wrote it, so an archive of one suite run on several Python versions
reads as one table. A figure the sampler can't measure is `null`, a list it
can't measure empty:

```json
{
  "profiler": "tachyon",
  "wall_time": 0.52,
  "sample_count": 8145,
  "top_function": {"function": "bench_big_ints", "file": "bench.py",
                   "line": 25, "self_time": 0.49},
  "top_instructions": [
    {"instruction": "BINARY_OP", "source": "total += i", "file": "bench.py",
     "line": 25, "share": 0.61, "unspecialized": true},
    {"instruction": "STORE_FAST", "source": "total", "file": "bench.py",
     "line": 25, "share": 0.21, "unspecialized": false}
  ],
  "unspecialized_share": 0.62
}
```

| Key                   | Meaning                                                                    |
| --------------------- | -------------------------------------------------------------------------- |
| `profiler`            | `"tachyon"` or `"pyinstrument"`.                                           |
| `wall_time`           | Seconds sampled: the timing loop minus `state.pause()` regions.            |
| `sample_count`        | Samples taken. Each sampler counts differently, so compare times instead.  |
| `top_function`        | The function with the most self time; `line` is `null` under pyinstrument. |
| `top_instructions`    | The hottest bytecode instructions; empty unless free-threaded 3.15+.       |
| `unspecialized_share` | Share of time in generic instructions; `null` unless free-threaded 3.15+.  |

A synthetic frame such as `<GC>` has `null` for `file` and `line`.
{class}`mew.cpu.CpuProfile` types the block. `context.python` records which
interpreter wrote a row; see {doc}`context`.
