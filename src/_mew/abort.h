// One channel for "stop this run and rethrow afterwards".
//
// GB's callback interfaces are effectively noexcept, and nanobind cannot hand a
// Python error to a frame not prepared for one, so every boundary (trampoline,
// reporter, manager) catches and stashes here; `run_benchmarks` rethrows once
// the loop returns. The trampoline also polls `mew_abort_pending()`, so a broken
// sink or a Ctrl-C stops the suite instead of measuring results it will discard.
//
// A body raising a plain `Exception` is not an abort: that is a per-benchmark
// `SkipWithError` and the run continues.

#pragma once

#include <exception>

// First one wins. Thread-safe: trampolines run on GB worker threads.
void mew_set_pending_abort(std::exception_ptr p);

bool mew_abort_pending();

// Return and clear (nullptr if none).
std::exception_ptr mew_take_pending_abort();
