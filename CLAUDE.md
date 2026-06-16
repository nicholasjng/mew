# Project Mew

Microbenchmarking Python code snippets, and reporting results.

## Tech stack

* Google Benchmark as a microbenchmarking library
* Python bindings via nanobind
* stdlib `argparse` for the CLI (no third-party CLI dependency)
* a small in-house ANSI module (`mew._console`) for terminal tables/colors — the runtime is zero-dependency
* vite + echarts as a visualization tool for a lightweight browser app

## Public Python API

Lightweight and small surface.

* Central: @mew.benchmark decorator to apply to a function to turn it into a benchmark.
* Need to pass a `State` object or similar that binds a `benchmark::State` C++ object.
* A @mew.parametrize decorator that defines a benchmark family mirroring the Google Benchmark concept. Needs to produce a sequence of callables that only take an input `State`, see previous.
* A reporter class that writes the results. This can be a wrapper around the C++ BenchmarkReporter class.

## Development setup

* Use `uv` and `uvx` for all things Python related, like dependency installation and removals, and linting.
* Prefer `prek` as a pre-commit runner tool.
* Add tests under tests/. Test the core functionality first.
* Use CMake for the Google Benchmark + nanobind setup, and `scikit-build-core` for PEP 517 wheel creation.
