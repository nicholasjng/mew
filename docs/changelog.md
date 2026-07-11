# Changelog

All notable changes to `mew` are documented here. Versions follow
[semantic versioning](https://semver.org/); until 1.0 the public API may still
change between minor releases.

## Unreleased

- `mew compare` marks statistically significant deltas with `(signif.)`, via a
  stdlib-only Mann-Whitney U test over per-repetition values (2+ repetitions
  required on both sides).

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
