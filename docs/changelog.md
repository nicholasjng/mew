# Changelog

All notable changes to `mew` are documented here. Versions follow
[semantic versioning](https://semver.org/); until 1.0 the public API may still
change between minor releases.

## Version 0.3.0 (unreleased)

### Added

- `mew run --summary-only` (`RichReporter(show_repetitions=False)`) shows each
  repeated benchmark only in the closing summary, not every repetition. Unlike
  Google Benchmark's `report_aggregates_only`, it only affects the console:
  result files keep every repetition, which `mew compare` needs.
- `--sample` samples with Tachyon, the sampling profiler in Python 3.15's
  standard library (`profiling.sampling`), through the new
  `mew.cpu.TachyonManager`. It needs no extra install and works on free-threaded
  3.15t, where pyinstrument would enable the GIL. Each sample counts as the time
  since the previous one: on GIL builds a C call that holds the GIL delays the
  sampler, and counting samples would all but drop it. `--sample-html` writes a
  flame graph with one root per benchmark. Its `<native>` frames mark C code
  calling back into Python, and on free-threaded builds each frame lists its
  bytecode instructions, specializations included. Before 3.15, `--sample`
  still uses pyinstrument; `mew.cpu.manager()` picks the sampler for the
  interpreter.
- `--sampler` and `mew.cpu.manager(sampler=...)` pick the CPU sampler: `auto`,
  the default, samples with Tachyon on 3.15+ and with pyinstrument before.
  `pyinstrument` also works on 3.15+ GIL builds, so a suite run on several
  interpreter versions can be profiled with one sampler throughout; it ignores
  `--sample-top-k`. `tachyon` fails before 3.15, and pyinstrument still fails
  on free-threaded builds.
- On free-threaded 3.15t, `--sample` also lists each row's hottest bytecode
  instructions in `cpu_profile.top_instructions`, with the source each covers,
  its line, its share of time and whether it stayed unspecialized, and gives the
  share of time spent in such unspecialized instructions
  (`unspecialized_share`). `--sample-top-k` sets how many (default 5). The
  console names the hottest below the row.
- A profiler manager's `get_result()` may return any JSON-serializable values,
  `None`, lists and dicts included. They reach `cpu_profile` with their JSON
  types; a value `json.dumps` rejects, such as a NumPy integer, fails the run,
  where 0.2.0 converted top-level numbers to floats.
- Every run records its interpreter in `context.python` (`version`,
  `implementation`, `gil_enabled`) and the mew that wrote it in
  `session.mew_version`, so an archive that mixes interpreters or mew releases
  can be split by them.
- Every run records the CPU model in `context.cpu_model`, such as `"Apple M2"`,
  or `null` where the OS doesn't name the CPU, as on Linux on Arm. `mew compare`
  warns when the compared files' models differ, as it does for the CPU count.
- `mew.gil_enabled()` tells whether the GIL is enabled right now: `False` on a
  free-threaded build, unless an extension module enabled it on import.
- Python 3.15 support, with a `cp315t` wheel for free-threaded 3.15t; the
  stable-ABI wheel already covers 3.15.
- `mew run` captures what benchmarks write to stdout and stderr, native writes
  included, and prints it to stderr after the run, so library warnings no longer
  break up the console table or corrupt JSON on stdout. `--capture=no`,
  `[tool.mew] capture = "no"` or `capture_output=False` shows it live instead.
- The console shows counters below each row, formatted like Google
  Benchmark's console (`bytes_per_second=1.2Gi/s`): SI or binary prefixes
  (`one_k`) and `/s` for rates. A counter carries this in an optional `format`.
  `CounterFlags.kStringifyExponent` is exposed for exponent notation.
- Decorators take `min_time` as `mew.run()` does: seconds (`0.5`, `"0.5s"`) or
  a fixed iteration count (`"100x"`), the same as `iterations=100`.

### Changed

- Result files and the console no longer hold Google Benchmark's aggregate rows
  (`_mean`, `_median`, `_stddev`, `_cv`), only each repetition: `mew compare`
  reduces repetitions itself, by its `--statistic`, and ignored them anyway.
  When the run ends, the console sums up each repeated benchmark in a second
  table, in the order the benchmarks were registered, also under
  `--random-interleaving`: the median real and CPU time, and the real time's
  stddev and cv, as `mew compare` computes them. `report_aggregates_only`
  is gone; it wrote files `mew compare` could not use. `mew compare` still
  skips the aggregate rows in older files.
- The console table fits the terminal. Its name column is as wide as the
  longest name the run will show, not all the width there is, and times are
  scaled to a readable unit (`305.03 ms`, not `305028333.33 ns`), as
  `mew compare` already did. What has no column, such as counters, a label the
  benchmark set and, under `--sample`, the sample count and hottest function,
  goes on an indented line below the row, filled to the terminal width instead
  of cut off; a skipped row's reason, such as a traceback, wraps within the
  timing columns. The `Label` column is gone, since a case's label is part of
  its name, and with it `RichReporter`'s `show_label` parameter.
- `mew compare` names the baseline in the table's title instead of its column
  header, and wraps each file's provenance line to the terminal width.
- `mew.cpu.write_html()` takes the profiler manager instead of its pyinstrument
  sessions, like `mew.memory.write_flamegraph()`.
- A row's `counters` is a list of `{"name", "value"}` records, plus the optional
  `format`, instead of a name-to-value mapping. DuckDB now infers the same type
  for it in every archive, whichever counters the archive contains. Archives
  written by 0.2.0 keep the old mapping, so don't query them together with
  newer ones.
- `cpu_profile` has the same keys and types for both samplers and every
  interpreter, typed by `mew.cpu.CpuProfile`: a figure a sampler can't measure
  is `null`, a list it can't measure empty. `top_function` is a record of
  `function`, `file`, `line` and `self_time`, which replaces
  `top_function_total_self_time`. Its `line` is the hottest line, or `null`
  under pyinstrument, which reported the `def` line.
- `cpu_profile.sample_count` is an integer instead of a float. DuckDB reads the
  column as `DOUBLE` in an archive that mixes both.
- Google Benchmark is pinned to `e662de9` (v1.9.5-144). `mew --version` and
  `mew.BENCHMARK_VERSION` report it as `v1.9.5-144-ge662de9a+mew`, without the
  `-dirty` suffix that mew's patches caused.
- On macOS, the thread CPU clock is read with `clock_gettime` instead of
  `thread_info`, which Google Benchmark kept for macOS 10.11: a read costs about
  110 ns instead of 510, and resolves 42 ns instead of 1 µs.
- On Apple Silicon Macs, `mew run` starts about a second sooner. macOS doesn't
  report the CPU frequency there, and Google Benchmark estimated it by
  busy-looping for a second; mew never reported it.
- `vcs_context()` lets jj snapshot the working copy, so uncommitted edits mark a
  jj checkout as dirty.
- The CPU and memory profiling passes run a single thread again, as in Google
  Benchmark, also for threaded benchmarks. `state.threads` reports 1 in these
  passes, in the body and in setup and teardown, so code sized by the thread
  count (for example a barrier) cannot wait for absent workers.
- `mew.run()` without a reporter prints mew's console table, like `mew run`,
  instead of Google Benchmark's console output. Google Benchmark's reporters
  are no longer used.
- Exit codes tell the outcomes apart: 0 for success, 1 when nothing matched, 2
  for a usage or environment error and 3 for a regression under
  `--exit-non-zero-on-regression`, which exited 2. Exit 2, with a one-line
  message instead of a traceback, now also covers an invalid `--statistic`, a
  missing `[tool.mew] setup` file, a sampler or profiler that can't run here, a
  missing, unreadable, truncated or malformed result file, an invalid
  `mew compare -k`, an unknown `@tag`, metric or key, `--strict` with threaded
  benchmarks on a GIL build, and a missing output directory, which exited 1 like
  "nothing matched". A benchmark or setup file that raises on import exits 2
  after its traceback.
- Library functions raise exceptions instead of `SystemExit`.
  `mew.compare.compare()`, `read_results()` and `session_summaries()` raise
  `ValueError`, `FileNotFoundError` or another `OSError`. `mew.cpu.manager()`,
  the sampler managers and `mew.memory.manager()` raise `ImportError` for a
  missing package and `RuntimeError` for a sampler that can't run here.
  `compare()`, `mew.regressions.render_panel()` and `report()` return 3 for a
  regression.
- The decorators and `mew.run()` check options by the same rules: a wrong type
  raises `TypeError` and an out-of-range value, NaN included, `ValueError`,
  where the decorators raised `TypeError` for both. The decorators also reject
  what Google Benchmark would ignore or misread: `iterations` with `min_time`
  or `min_warmup_time`, whose time budget it ignored; `iterations=2.5`, which
  ran 2 iterations; `repetitions=True`; an infinite time; `use_real_time` with
  `use_manual_time`, where manual time won; and `min_warmup_time` without
  `min_time` in seconds, which did nothing. A misspelled `unit` fails at
  decoration instead of during the run.
- `mew.run()` and `--session-tag` reject the tag `latest`, which
  `mew compare path@latest` reserves for the newest session, and an empty tag,
  which was dropped.
- Unknown keys in `[tool.mew]`, `[tool.mew.regressions]` and its `allow` rules
  are errors that suggest the likely key, instead of being ignored. This
  includes free-form keys such as `note = "..."` in an allow rule. An invalid
  `statistic` fails every command, not only `mew compare`.
- Config keys are spelled with dashes only, as TOML has them: `python_files`
  and `[tool.mew.regressions] default_threshold`, the spelling 0.2.0 required,
  must become `python-files` and `default-threshold`. The error names the
  dashed key.
- In the context, the last write wins: `set_context("dataset.size", 1)`
  replaces a `dataset` that is not a dict instead of raising. Values are
  deep-copied, so changing a dict or list after setting it no longer changes
  what runs record.
- `mew compare` measures noise (`±N% (!)`) as the stddev over the mean of the
  repetitions, whatever `--statistic` is; `--statistic min` could inflate it. It
  shows the same rows with or without a regression gate: the benchmarks the
  first file and the baseline share, with dashes for a file in between that
  lacks one. `path@tag` is split after a `.json`, `.jsonl` or `.jsonl.gz`
  extension instead of by whether a file of the full name exists.
  `Sample.time_unit` is `None` for metrics that aren't times.
- A memory manager's `stop()` must return `None` or a dict with `peak_bytes`
  and `total_allocations`, and optionally `total_bytes` and `net_heap_growth`.
  A missing or unknown key fails the run with `TypeError` instead of being
  zeroed or dropped, and `None` leaves the row without a `memory` block instead
  of one of zeros. A profiler's `get_result()` of `{}` becomes an empty
  `cpu_profile` instead of none.
- mew resumes a `state.pause()` held over the end of the loop before it stops
  the profiler, so a manager's `pause()` and `resume()` calls are balanced and
  its teardown never finds it paused.
- A CPU profiling pass that caught no sample reports a profile with
  `sample_count` 0 and a `null` `top_function`, shown as `0 samples`, instead
  of no `cpu_profile`. `mew.cpu.manager()` checks `interval` and `top_k` for
  every sampler, and `top_k` must be an integer. `mew.cpu.write_html()` raises
  `TypeError` for a manager it can't render instead of treating it as
  pyinstrument.

### Removed

- `State.keep_running_batch()`; `state.batches()` does the same.

### Fixed

- After a `KeyboardInterrupt` or a failing reporter, benchmarks that did not run
  no longer reach result files as `aborted` rows.
- Returning or breaking out of the benchmark loop, including in the final
  iteration, reports an incomplete-loop error instead of near-zero time.
- `state.pause()` outside the benchmark loop raises `RuntimeError` instead of
  adding a raw clock reading to the result.
- Nested `state.pause()` scopes keep timing and CPU sampling paused until the
  outer scope exits; an inner scope does nothing. 0.2.0 stopped the stopped
  timer again, counting the time since it last started twice. Re-entering a
  scope object inside its own `with` block raises `RuntimeError`. A scope left
  open no longer disables pausing in later benchmarks, and a scope held open
  over the end of the loop, for example by an `ExitStack`, keeps its paused time
  out of the result.
- Benchmarks whose timed run fails or is skipped no longer get memory and CPU
  profiling passes, so they add nothing to flame graphs or CPU HTML reports. A
  pass that ends early, for example on Ctrl-C, still stops its profiler.
- `--sample-html` and `mew.cpu.write_html()` warn on stderr when no profiling
  pass collected a sample, like `--flamegraph`, instead of writing no report
  without a word.
- On a free-threaded build run with the GIL enabled (`PYTHON_GIL=1`),
  `--profile-memory` records Python's allocations instead of none.
- The memory pass's `iterations` includes the overshoot of batched loops
  (`state.batches()`), so `allocations_per_iteration` is no longer inflated.
- A second `for _ in state:` loop after the first has finished no longer adds
  time to the result.
- `--regression-threshold` overrides `[tool.mew.regressions] default-threshold`
  instead of being silently replaced by it.
- Regression allowlist patterns match brackets literally, so `*[algo=bubble]`
  selects that case instead of acting as a character class.
- Calling `state.set_label()` in a parametrized family only sets the row's
  `label`; rows stay named by their case label, so cases that set the same
  label no longer collapse into one benchmark in `mew compare`. Each case is now
  registered with Google Benchmark on its own, so the raw `name` reads
  `f[n=1]/case:1/…` and `family_index` counts cases rather than families.
- `mew compare` shows `×1.000` instead of `×inf` when both sides are zero, and
  scales the `memory.peak_bytes` stddev column like its values.
- The memory flame graph's header totals cover every benchmark, not the first.
- Piped console output, as in `mew run | tee run.log`, is as wide as the
  terminal it ends up on, instead of 80 columns: when stdout is not a terminal,
  mew asks stderr and then the controlling terminal for the width.
- An output path ending in `.GZ` or `.Gz` is gzip-compressed, as reading it back
  assumes, instead of written as plain text.
- Benchmark files symlinked into a benchpath are selected by `mew list` and
  `mew run`.
- A decorator rejected for a duplicate `name=` leaves the function free for a
  corrected registration.
- A benchmark that raises is reported once, in its row's skip message, instead
  of also as an "Exception ignored in" message.
- `--help` honors `FORCE_COLOR`, and shell completions no longer show `%%`.
- On Python 3.14+, argparse no longer colors `--help` under mew's own colors,
  which mixed two color schemes and colored redirected help when
  `PYTHON_COLORS` was set.
- A CPU profiling pass that ends before its loop, for example by skipping, no
  longer reports the previous benchmark's profile.
- Wheels include the licenses of the code they link in, Google Benchmark,
  nanobind and nanobind's `tsl::robin_map`, and the license expression names
  them: `Apache-2.0 AND BSD-3-Clause AND MIT`.
- A build given Google Benchmark's source through
  `FETCHCONTENT_SOURCE_DIR_GOOGLEBENCHMARK`, as an offline package build is,
  stops unless mew's patches are applied to it, instead of building against
  unpatched Google Benchmark.

## Version 0.2.0 (September 24, 2026)

This release focuses mew on benchmark execution, reporting, and comparison.
Profiling uses Google Benchmark's manager interfaces; variant orchestration and
native-profiler integrations have been removed.

### Added

- `mew.run()` accepts custom memory and profiler managers. Profiling runs in
  separate, untimed passes scoped to the benchmark timing loop; summaries are
  attached to each result under `memory` and `cpu_profile`. Profiler summaries
  and `pause()` / `resume()` hooks are optional.
- Public `MemoryManager`, `ProfilerManager`, `ProfilerResultProvider`, and
  `PausableProfiler` protocols document the manager contracts. `MemoryMetrics`
  describes the fixed memory-result schema.
- `mew.machine_context()` and `mew.vcs_context()` provide reusable provenance.
  `[tool.mew] setup` imports a project setup file before benchmark discovery,
  allowing context to be configured once for every CLI run.
- `mew compare path@<tag>` selects the session(s) written with that
  `--session-tag` from a multi-session file; `path@latest` names the newest.
  `mew sessions FILE` lists a file's sessions, newest first.
- `mew compare` marks statistically significant deltas with `(signif.)`, using
  a stdlib-only Mann–Whitney U test over per-repetition values. Both sides need
  at least two repetitions.
- `threads` accepts a sequence of thread counts (`threads=[1, 2, 4, 8]`) and
  runs the benchmark once per count on a free-threaded interpreter.
- `mew run --memory-iterations N` (and `mew.run(memory_iterations=)`) caps the
  memory-profiling pass at `N` iterations instead of the fixed 16.
- Result rows carry `benchmark`, the addressable name mew uses in `mew list`
  and `-k` (`file.py::func[label]`), so readers and SQL queries no longer parse
  option suffixes out of `name`.
- `CounterFlags` is available from the package root. `CounterOneK` and the new
  `one_k=` argument to `State.set_counter()` select decimal or binary scaling
  in Google Benchmark's native console output.

### Changed

- Reporter callbacks now receive plain `BenchmarkResult` dictionaries directly
  from the native runner. The public native `Run`, `RunType`, and
  `BenchmarkName` wrappers have been removed.
- `Reporter.report_context()` returns `None`; an exception from any reporter or
  manager callback aborts the run. Reporter finalization now also runs for
  suites whose selected benchmarks were all skipped.
- Session identity and provenance are stored in separate `session` and
  `context` blocks on every row, in JSON and JSONL alike; the JSON document
  lists both at its top level as well. A multi-session archive
  contributes its newest session to `mew compare` unless a `@<tag>` selector
  says otherwise; sessions sharing a tag pool as repetitions.
- CPU and memory profiling are driven by Google Benchmark itself instead of
  Python-side result projection. `State.pause()` regions are excluded from CPU
  sampling as well as timing.
- The generated native stub is written to the build tree. Wheels install the
  checked-in formatted stub; maintainers update it explicitly with the
  `update_mew_core_stub` CMake target, so dependency builds do not dirty local
  checkouts.
- Public documentation and docstrings were reorganized and tightened around
  the reduced API.

### Removed

- The `--variant` runner, variant worker processes, and `[tool.mew]` variant
  configuration. Run independent environments explicitly, one result file per
  side, and compare the files.
- `mew compare --by` / `--baseline` pivots. Use one file per side, or
  `--session-tag` plus `path@<tag>` selectors within one archive; richer
  slicing is a DuckDB query away (see the reporters guide).
- The `mew profile` command and bundled `perf`, `py-spy`, and `xctrace`
  adapters. The guide now documents invoking native profilers externally;
  `mew run --sample` and `--profile-memory` remain for in-process profiling.
- Arbitrary custom comparison statistics. The built-in reducers remain
  available for persisted-result comparison.
- Dynamic benchmark-name shell completion and its cache. Generated completion
  scripts remain static and side-effect free.
- `[tool.mew.session-tag]` command execution. Use an explicit `--session-tag`
  or populate version-control context from `[tool.mew] setup`.
- `thread_range` and `dense_thread_range`; pass the thread counts to `threads`.
- `mew compare --regressions-config`; allow rules live in the project's
  `pyproject.toml` only.

### Fixed

- Thread counts remain distinct in displayed benchmark names and comparison
  samples instead of being merged as repetitions.
- The CPU and memory profiling passes run with the benchmark's configured thread
  count. They previously ran a single thread while `state.threads` still
  reported the configured count.
- Historical comparison files with missing benchmarks no longer hide
  candidate-versus-baseline regressions.
- Path selectors apply their filters only within the selected file or directory,
  including selectors read from standard input.
- Grouped sessions with different declared time units normalize measurements
  before calculating statistics.
- Memray now traces Python allocators on free-threaded CPython, where object
  allocation otherwise bypassed its system-allocator hooks.
- Memray no longer reports peak-live allocation bytes as cumulative
  `total_bytes`; the optional field is omitted when it cannot be computed
  cheaply.
- Optional profiler `get_result()` methods and `None` results are handled
  correctly; malformed manager result dictionaries raise descriptive errors.
- Duplicate low-level manager registration is rejected safely.
- Invalid global run options and malformed regression thresholds or allow rules
  are rejected instead of being silently ignored or coerced.
- Memray capture files are closed after reading their metadata.
- Reusing a `JSONReporter` no longer corrupts comma placement, and owned output
  streams are reset after finalization.
- Native warning flags are selected correctly for MSVC, and Google Benchmark
  patches are applied reproducibly across platforms.
- CPU profiling excludes pyinstrument's own frames from hottest-function
  summaries. It is rejected on free-threaded Python instead of silently
  enabling the GIL.

## Version 0.1.1 (Jul 30, 2026)

Fixes the readthedocs build that was silently broken, no user-facing API changes.

## Version 0.1.0 (Jul 30, 2026)

First public release. `mew` is a microbenchmarking library and CLI for Python,
built on [Google Benchmark](https://github.com/google/benchmark) via
[nanobind](https://github.com/wjakob/nanobind). The runtime has no third-party
dependencies.

### Writing benchmarks

- `@mew.benchmark` registers a function taking a `State` as a benchmark, with
  per-benchmark measurement options (`min_time`, `repetitions`, `iterations`,
  `unit`, threading, tags).
- `@mew.parametrize` and `@mew.product` register benchmark families, mirroring
  Google Benchmark's ranged benchmarks with Python-level cases and labels.
- `State` exposes the timing loop plus Google Benchmark's `pause_timing`,
  `set_counter`, `set_items_processed`, `set_bytes_processed`,
  `skip_with_error` / `skip_with_message`, and manual iteration timing.
- `mew.set_context` / `update_context` / `get_context` / `clear_context` attach
  arbitrary run context to reported rows.

### CLI

- `mew run` discovers `bench_*.py` files and runs them, with regex (`-k`), tag
  (`-t`), literal, and stdin-driven selection.
- `mew list` (`mew ls`) enumerates discovered benchmarks without running them.
- `mew compare` diffs result files, with `--regression-threshold`,
  `--exit-non-zero-on-regression`, an allowlist, and `--by` grouping for
  sessions and variants.
- `mew profile` profiles benchmarks out-of-process for native frames via
  xctrace (Instruments), py-spy, or perf.
- `mew completions` prints a shell-completion script; completion callbacks read
  a cached benchmark index and never import benchmark files.
- `--variant name=path` runs a variant in its own subprocess for A/B comparison.
- `--session-tag` labels a run's output, derived from `jj` or `git describe` by
  default.

### Output and reporting

- Reporters for a formatted terminal table (`RichReporter`), JSON
  (`JSONReporter`), and JSONL archives (`JSONLReporter`, with gzip and
  `--append` support), plus `Fanout` to write several at once.
- Rows are reported as each benchmark completes, rather than buffered until the
  end of the suite.
- `Reporter` is subclassable for custom sinks.

### Profiling

- `mew run --sample` for in-process CPU sampling with `pyinstrument`
  (`mew-bench[cpu]`), with `--sample-html` output.
- `mew run --profile-memory` for `memray` allocation tracking
  (`mew-bench[memory]`, non-Windows), with `--flamegraph` output.

### Configuration

- A `[tool.mew]` table in the nearest `pyproject.toml` configures discovery
  paths, file patterns, session tagging, and regression rules. Measurement
  settings stay on decorators and CLI flags.

### Packaging

- Wheels for CPython 3.11 and a `cp312` stable-ABI wheel covering 3.12+, on
  manylinux and musllinux (x86_64, aarch64), macOS arm64, and Windows amd64.
- Free-threaded wheels for CPython 3.14t; the extension declares
  `Py_MOD_GIL_NOT_USED` and does not re-enable the GIL on import.
- Typed: the package ships `py.typed` and a stub for the C++ extension.
