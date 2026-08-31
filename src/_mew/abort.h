// Store exceptions that cannot unwind through Google Benchmark callbacks.

#pragma once

#include <exception>

// First one wins. Thread-safe: trampolines run on GB worker threads.
void mew_set_pending_abort(std::exception_ptr p);

bool mew_abort_pending();

// Return and clear (nullptr if none).
std::exception_ptr mew_take_pending_abort();
