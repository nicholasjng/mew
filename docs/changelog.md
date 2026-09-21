# Changelog

All notable changes to `mew` are documented here. Versions follow
[semantic versioning](https://semver.org/); until 1.0 the public API may still
change between minor releases.

## Version 0.2.0 (unreleased)

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
- `mew compare --by FIELD` pivots one result file on any session or context
  field, replacing the variant-specific comparison path. Untagged runs can be
  grouped by `context.vcs.commit`.
- `mew compare` marks statistically significant deltas with `(signif.)`, using
  a stdlib-only Mann–Whitney U test over per-repetition values. Both sides need
  at least two repetitions.
- `dense_thread_range=(min, max, stride)` runs a benchmark at evenly spaced
  thread counts on a free-threaded interpreter.
- `mew run --memory-iterations N` (and `mew.run(memory_iterations=)`) caps the
  memory-profiling pass at `N` iterations instead of the fixed 16.
- Result rows carry `name_parts`, Google Benchmark's decomposed run name, so
  readers no longer parse option suffixes out of `name`.
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
  `context` blocks. JSONL rows carry both blocks, and result readers support
  selecting and combining sessions from append-only archives.
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
  configuration. Run independent environments explicitly and compare them via
  shared context plus `mew compare --by`.
- The `mew profile` command and bundled `perf`, `py-spy`, and `xctrace`
  adapters. The guide now documents invoking native profilers externally;
  `mew run --sample` and `--profile-memory` remain for in-process profiling.
- Arbitrary custom comparison statistics. The built-in reducers remain
  available for persisted-result comparison.
- Dynamic benchmark-name shell completion and its cache. Generated completion
  scripts remain static and side-effect free.
- `[tool.mew.session-tag]` command execution. Use an explicit `--session-tag`
  or populate version-control context from `[tool.mew] setup`.

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
