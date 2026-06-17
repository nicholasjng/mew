// Cross-TU channel for BaseException interrupts (KeyboardInterrupt, SystemExit)
// raised inside benchmark bodies. The trampoline stashes the exception and every
// later trampoline invocation short-circuits, so RunSpecifiedBenchmarks winds
// down quickly; `run_benchmarks` rethrows it after the loop returns.

#pragma once

#include <exception>

// Stash `p` as the pending interrupt unless one is already set. Thread-safe.
void mew_set_pending_interrupt(std::exception_ptr p);

// True when an interrupt is pending; benchmark trampolines skip immediately.
bool mew_interrupt_pending();

// Return and clear the pending interrupt (nullptr if none). Thread-safe.
std::exception_ptr mew_take_pending_interrupt();
