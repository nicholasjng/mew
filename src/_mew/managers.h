// Cross-TU channel for the GB manager bindings.
//
// `managers.cpp` owns the MemoryManager / ProfilerManager trampolines;
// `reporter.cpp` needs to drain their pending exception after the run loop, and
// `state.cpp` needs to suspend the active profiler across `state.pause()`.

#pragma once

#include <exception>

// Return and clear the first exception a manager callback raised (nullptr if
// none). GB's manager interfaces are noexcept, so a raising Python manager is
// stashed and rethrown from `run_benchmarks`, exactly as reporter callbacks are.
std::exception_ptr mew_take_pending_manager_exception();

// Suspend / resume the registered profiler manager, if any, around a
// `state.pause()` region. No-ops when no manager is registered or when the
// registered one does not implement the hooks.
void mew_profiler_pause();
void mew_profiler_resume();
