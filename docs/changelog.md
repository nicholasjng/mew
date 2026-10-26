# Changelog

Versions follow [semantic versioning](https://semver.org/); until 1.0, minor
releases may break the API.

## Unreleased

### Breaking changes

- `mew run --memory-iterations` and `run(memory_iterations=)` are gone: the
  memory pass runs Google Benchmark's 16 iterations, or fewer if the benchmark
  runs fewer.
- `mew compare` always reduces repetitions to their median: `--statistic`,
  `[tool.mew] statistic` and `compare(statistic=)` are gone. For another
  reduction, query the result file, e.g. with DuckDB.
- `mew compare --key func` and `compare(key=)` are gone: rows match by their
  full name. For an A/B comparison, run one benchmark file against each side;
  see the A/B guide.
- `mew.Fanout` is gone: pass a list to `mew.run(reporter=[...])`.
- `mew compare` no longer takes `--metric iterations`: Google Benchmark picks
  the count to fill `min_time`, so it only mirrored the time.
- The JSON document is `{"benchmarks": [...]}`: its rows carry `run` and
  `context` themselves, as in JSONL, so the document no longer repeats them.
- `set_context()`, `update_context()`, `get_context()`, `clear_context()` and
  dotted context keys are gone. Pass `mew.run(context={...})`; for `mew run`,
  define `context()` in the `[tool.mew] setup` file, which `mew run` calls
  once before the first benchmark. Benchmark files can no longer add context
  when imported.
- Context values JSON can't hold are stored as their type's qualified name
  instead of their `str()`; paths as their string.
- A row's `session` block is `run`: `{id, date, name}`, with `date` in UTC
  with milliseconds (`2026-11-08T12:24:23.559Z`). `--session-tag` and
  `session_tag=` are `--run-name` and `run_name=`; `mew sessions` is `mew log`;
  `SessionInfo`, `SessionSummary` and `session_summaries()` are `RunInfo`,
  `RunSummary` and `run_summaries()`; `Reporter.report_runs()` is
  `report_results()`.
- Result-file selectors are gone: `mew compare` and `mew show` no longer take
  `file@<tag>` or `file@latest`, and `latest` is a valid run name again. A
  file is one sample: `mew compare` pools every run in it, and warns when the
  runs differ in machine, interpreter, mew version or commit. `mew show`
  prints each run. To compare runs of one archive, extract them first, e.g.
  with DuckDB.
- `session.mew_version` is `context.mew`, and `mew compare` warns when it differs.
- Result format v1: every row of a result file starts with `"schema": "v1"`,
  and mew rejects rows without it, such as files from earlier versions.
- A row's `name` is mew's addressable name (it was `benchmark`); Google
  Benchmark's `name` and `run_name`, `run_type`, `aggregate_name`,
  `family_index`, `per_family_instance_index`, `real_accumulated_time` and
  `cpu_accumulated_time` are gone. `repetition_index` is `repetition`.
- Times are always nanoseconds: `time_unit`, the `unit=` option and
  `mew.TimeUnit` are gone.
- `skipped` and `skip_message` are one `skip` record, `{reason, message}` with
  `reason` `"error"` or `"skipped"`, or `null` for a measured row.
- The console's repetition summary lists benchmarks in the order they first
  reported, not registration order.
- `cpu_profile.top_instructions`, `unspecialized_share`, `mew.cpu.TopInstruction`,
  `mew.cpu.reads_instructions()` and the `top_k` option of Tachyon are gone. On
  free-threaded builds, Tachyon's flame graph still lists each frame's instructions.
- Allow rules take `threshold = inf` instead of `ignore = true`, and every rule
  needs a `threshold`. `Verdict.IGNORED` and `AllowRule.ignore` are gone; an
  exempt benchmark past the default threshold shows as over-but-allowed.
- `@mew.product` is gone; use `@mew.parametrize(mew.grid(...))`. Axes named
  like options, such as `threads`, are no longer taken as options.
- `mew run --cpu-profiler <preset>` replaces `--sample`, `--sampler`,
  `--sample-interval` and `--sample-top-k`; `--memory-profiler <preset>`
  replaces `--profile-memory`. The built-in presets are `sample` and `memray`.
- `mew run --profile-dir DIR` replaces `--sample-html` and `--flamegraph`, and
  writes to `DIR/cpu` and `DIR/memory`.
- Memory profiling writes one flame graph per benchmark,
  `DIR/memory/<n>-<function>.html`, and an `index.html` that links them,
  instead of one report.
- `mew.ProfilerResultProvider` and `mew.PausableProfiler` are merged into
  `mew.ProfilerManager`.
- `mew.cpu.write_html()` and `mew.memory.write_flamegraph()` are replaced by
  `manager.write_outputs(directory)`. `mew.memory.manager()` is gone: create a
  `mew.memory.MemrayManager()`, which raises `ImportError` without memray.
- Benchmark names are relative to the project root (the nearest
  `pyproject.toml` above the file), not the working directory.
- Rows no longer record `session.host`; `SessionSummary.host` and the `Host`
  column of `mew sessions` are gone. Use `mew.set_context("host", ...)`.
- A raising benchmark's `skip_message` is `Type: message` plus notes; the full
  traceback goes to stderr.
- `mew list --names-only` is gone; `mew run --stdin` lines are `file.py::name`
  selectors, as `mew list` prints them.
- `-o stdout` is gone (use `-o -`). `--append` needs a `.jsonl` sink.
- `[tool.mew] capture` is gone.
- `pNN` percentiles are gone from `--statistic` and `[tool.mew] statistic`.
- `State.range()` and `State.range_size` are gone; a case's raw `name` drops
  `/case:N`.
- Abbreviated long options, such as `--rep`, are rejected.
- `python-files` patterns match file names only.
- Stacking `@benchmark` and `@parametrize` raises `ValueError`.
- `mew compare --key` takes only `name` or `func`.
- `mew.gil_enabled()` and `mew.is_free_threaded_build()` are gone; every row
  records both in `context.python`.
- `mew.memory.Capture`, `MemrayManager.captures` and `mew.regressions.report()`
  are gone; `render_panel()` returns the panel's text and exit code.
- `mew.compare.Sample` is private. `mew.config.Config.project_root` is always a
  `Path`.
- Result files written by 0.2.0 are no longer read.
- On a GIL build, threaded benchmarks left out of the run no longer get a
  skipped row.
- `mew compare` takes the baseline first: `mew compare baseline.json head.json`.
  With more files, the second is the one gated.
- `mew compare --stddev` and `compare(show_stddev=)` are gone: each value
  carries its spread.

### Added

- `mew run --context <path>=<value>` records a string in one run's context, over
  the setup file's `context()`. A dot nests into the hook's tables, as in DuckDB,
  and a quoted key keeps its dots: `--context '"a.b"=1'`.
- Rows carry `params`, a family case's parameters; values JSON can't represent
  are stored as their type's qualified name, such as `"torch.Tensor"`.
- `mew.grid(**axes)` gives every combination of some values as cases for
  `@parametrize`. Join grids with `+` or filter one for partial grids.
- Profiler presets: `[tool.mew.profilers.<name>]` names a manager class and its
  options. `--cpu-profiler module:Class` and `--memory-profiler module:Class`
  run a class directly.
- `write_outputs(directory)` on `MemoryManager` and `ProfilerManager`, for a
  custom manager's files.
- `mew show FILE[@tag]` prints a stored session as `mew run` printed it.
- `mew compare -m counters.<name>` compares and gates on a counter.
  `[[tool.mew.counters]]` rules set which direction is better.

### Changed

- Discovery skips hidden directories, virtualenvs, `__pycache__`, `build`,
  `dist` and `node_modules` below the searched paths.
- `mew compare` computes exact Mann-Whitney p-values for up to 20 repetitions
  per side without ties.
- The `mew compare` ratio column shows `—` when a counter is negative.
- A memory manager's `stop()` may return extra keys; mew ignores them.
- `mew compare` shows each value's spread inline, as `±N%`, red past 25%.
- `mew compare` context lines show a run's name and date, how many runs a file
  pooled, and only the context values that differ between the files.
- `mew compare` reports skipped benchmarks with their reason instead of as missing.
- `mew compare` names a file prefix all rows share once, in the title.
- `mew compare` sums up the second file against the baseline under the table.
- Errors and warnings are shorter, in the form `mew: error: <what>: <problem>`
  and `mew: warning: …`, with a fix, where one helps, on a `hint:` line. The
  CLI prints mew's own Python warnings the same way, without a source location.
- A mistyped command or a value outside an option's fixed set reads
  `invalid command: 'rnu'` or `--format: invalid value 'jsn'`, with the closest
  match on a `hint:` line, on every Python version. Help shows such values as
  `(rich | json | jsonl)`.

### Fixed

- `mew log` shows run ids long enough to tell apart runs from the same minute.
- `MemrayManager` raised a `ResourceWarning` when it was garbage-collected.
- A benchmark defined by code without a module, as from `exec()`, was named
  `None::bench_x`; it is now `<unknown>::bench_x`.
- Memory profiling on GIL builds saw only the memory arenas Python's allocator
  requested, not the objects in them: `peak_bytes` and allocation counts for
  object-heavy code were about 35x too low. Python's allocator is now traced on
  every build; the memory pass gets slower in proportion to the allocations.
- A profiler manager subclass that wraps `start()` named every capture after
  its own method.
- `mew compare` gave counter deltas with a negative or zero baseline the wrong
  sign.
- memray merged benchmarks whose loop runs in a shared helper; Tachyon cut their
  stacks at the helper.
- Calling `mew.cli.main()` twice in one process leaked context values into the
  second run and dropped benchmarks registered before it.
- A failed `git status` recorded the tree as clean.
- `--min-time` accepted non-ASCII digits, such as `١٠x`, and ran 0 iterations.
- `interval = true` in a preset meant one second.
- `mew = []` in `pyproject.toml` silently loaded the defaults.
- A directory named `pyproject.toml` stopped the config search.
- Tracebacks instead of an exit-2 message for: a `--profile-dir` that isn't a
  directory, a `[tool]` or `[tool.mew]` that isn't a table,
  `@mew.benchmark("name")`, and non-string `ids`.
- Result files were read in the locale's encoding instead of UTF-8.
- Some read errors didn't name the file.
- `mew compare -k` with no match reported that the files didn't overlap.
- The regression panel didn't name the stricter rule that failed a benchmark.
- bash completion split file names with spaces. zsh and fish didn't complete
  `-h` and `--version`, and fish didn't complete `mew completions`' shells.
- Usage errors on stderr were colored when stdout was a terminal.
- pyinstrument's `top_function` merged same-named functions from same-named files.

## Version 0.3.0 (Oct 21, 2026)

### Breaking changes

- Result files and the console no longer contain Google Benchmark's aggregate
  rows; `report_aggregates_only` is gone. The console summarizes repeated
  benchmarks in a closing table.
- A row's `counters` is a list of `{"name", "value", "format"}` records instead
  of a mapping.
- `cpu_profile` has the same keys for every sampler (`mew.cpu.CpuProfile`).
  `top_function` is a record that replaces `top_function_total_self_time`, and
  `sample_count` is an integer.
- Exit codes: 0 success, 1 nothing matched, 2 usage or environment error, 3
  regression (was 2). Many more errors exit 2 with one line.
- Library functions raise exceptions instead of `SystemExit`.
- Decorators and `mew.run()` check options alike: a wrong type raises
  `TypeError`, a bad value `ValueError`. Contradictory options, such as
  `iterations` with `min_time`, are rejected.
- Unknown keys in `[tool.mew]` tables are errors. Keys are dashed only:
  `python-files`, `default-threshold`.
- `set_context()` deep-copies values, and the last write wins for dotted keys.
- The session tag `latest` and empty tags are rejected.
- A memory manager's `stop()` must return `None` or a dict with `peak_bytes`
  and `total_allocations`.
- `mew.cpu.write_html()` takes the profiler manager.
- The console's `Label` column and `RichReporter(show_label=...)` are gone.
- Profiling passes run one thread; `state.threads` is 1 in them.
- `mew.run()` without a reporter prints mew's console table.
- `State.keep_running_batch()` is gone (use `state.batches()`), and so is
  `mew.regressions.load_config()` (use `mew.config.load().regressions`).

### Added

- Tachyon, Python 3.15's sampling profiler, as CPU sampler
  (`mew.cpu.TachyonManager`), also on free-threaded 3.15t. `--sampler` picks it.
- On free-threaded 3.15t, `cpu_profile.top_instructions` and
  `unspecialized_share`.
- `mew run --summary-only` (`RichReporter(show_repetitions=False)`).
- `mew run` captures what benchmarks print, native output included, and prints
  it after the run; `--capture=no` shows it live.
- The console shows counters below each row, formatted as Google Benchmark
  does. `CounterFlags.kStringifyExponent` is exposed.
- `context.python`, `context.cpu_model` and `session.mew_version` in every row.
  `mew compare` warns when CPU models differ.
- `mew.gil_enabled()`; `mew.Terminal` is public.
- Decorators take `min_time` as `mew.run()` does; durations take `ms` and `m`.
- A profiler's `get_result()` may return any JSON-serializable values.
- `mew.config.Config.regressions`.
- Python 3.15 support, with a `cp315t` wheel.

### Changed

- The console table fits the terminal and scales times to a readable unit.
- `mew compare` names the baseline in the title, and measures noise as the
  stddev over the mean.
- Google Benchmark is pinned to `00aed1d` (v1.10.0-4).
- macOS: CPU time reads take about 110 ns instead of 510, with 42 ns
  resolution instead of 1 µs. Apple Silicon runs start about a second sooner.
- `vcs_context()` sees uncommitted edits in a jj checkout.

### Fixed

- After Ctrl-C or a failing reporter, benchmarks that didn't run appeared as
  `aborted` rows.
- Leaving the loop early reported near-zero time; a second `for _ in state:`
  loop added time.
- Nested or leftover `state.pause()` scopes miscounted time;
  `state.pause()` outside the loop added a raw clock reading.
- Failed or skipped benchmarks got profiling passes; a pass that ended before
  its loop reported the previous benchmark's profile.
- A CPU report with no samples was silently not written.
- Memory profiling recorded no Python allocations on a free-threaded build
  with `PYTHON_GIL=1`.
- `allocations_per_iteration` was inflated for `state.batches()` loops.
- `--regression-threshold` was overridden by `default-threshold`.
- A benchmark's own `min_warmup_time` needed a `min_time` too.
- Allowlist patterns treated brackets as character classes.
- `state.set_label()` in a family merged its cases in `mew compare`.
- `mew compare` showed `×inf` for 0 vs 0.
- The memory flame graph's header totals covered only the first benchmark.
- Piped console output was 80 columns wide.
- `.GZ` output paths weren't compressed.
- Symlinked benchmark files weren't discovered.
- A raising benchmark was reported twice.
- `--help` ignored `FORCE_COLOR`; completions showed `%%`; on 3.14+ help mixed
  two color schemes.
- Wheels lacked the licenses of bundled code; the license expression is now
  `Apache-2.0 AND BSD-3-Clause AND MIT`.
- A build from `FETCHCONTENT_SOURCE_DIR_GOOGLEBENCHMARK` could skip mew's
  patches.
- The `mew.State` stub didn't match the runtime type.

## Version 0.2.0 (September 24, 2026)

### Breaking changes

- Removed: the `--variant` runner, `mew compare --by` and `--baseline`,
  `mew profile` with its perf, py-spy and xctrace adapters, dynamic shell
  completion, `[tool.mew.session-tag]` commands, `thread_range` and
  `dense_thread_range`, `mew compare --regressions-config`, and custom
  comparison statistics.
- Reporters receive plain `BenchmarkResult` dicts; the `Run`, `RunType` and
  `BenchmarkName` wrappers are gone. An exception in a reporter or manager
  aborts the run.
- Rows carry separate `session` and `context` blocks.

### Added

- Custom memory and profiler managers in `mew.run()`, with the
  `MemoryManager` and `ProfilerManager` protocols.
- `mew.machine_context()`, `mew.vcs_context()` and `[tool.mew] setup`.
- `mew compare path@<tag>` and `path@latest`; `mew sessions FILE`.
- `(signif.)` marks significant deltas in `mew compare`.
- `threads=[1, 2, 4, 8]` runs once per thread count.
- `mew run --memory-iterations N`.
- Rows carry `benchmark`, the name `mew list` prints.
- `CounterFlags`, `CounterOneK` and `set_counter(one_k=...)`.

### Fixed

- Thread counts were merged as repetitions in names and comparisons.
- Benchmarks missing from a history file could hide regressions.
- Path selectors' filters leaked beyond the selected path.
- Sessions with different time units weren't normalized before statistics.
- memray missed Python allocations on free-threaded CPython, and reported peak
  bytes as `total_bytes`.
- Invalid run options, thresholds and allow rules were silently accepted.
- Reusing a `JSONReporter` corrupted its output.
- pyinstrument's own frames showed in summaries; it silently enabled the GIL on
  free-threaded Python.

## Version 0.1.1 (Jul 30, 2026)

- Fixed the documentation build.

## Version 0.1.0 (Jul 30, 2026)

First public release: a microbenchmarking library and CLI for Python on
[Google Benchmark](https://github.com/google/benchmark), with no runtime
dependencies.

- `@mew.benchmark`, `@mew.parametrize` and `@mew.product`, with Google
  Benchmark's per-benchmark options and `State` API.
- `mew run`, `mew list`, `mew compare` with a regression gate, and
  `mew completions`.
- Console, JSON and JSONL reporters.
- CPU sampling with pyinstrument and memory profiling with memray.
- `[tool.mew]` configuration in `pyproject.toml`.
- Wheels for CPython 3.11, 3.12+ (stable ABI) and free-threaded 3.14t, on
  Linux, macOS arm64 and Windows amd64.
