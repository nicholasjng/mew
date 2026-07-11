// Cross-TU channel for the GB manager bindings.
//
// `managers.cpp` owns the MemoryManager / ProfilerManager trampolines;
// `state.cpp` suspends the profiler across a `state.pause()` region.
// A raising manager goes through the shared abort channel (`abort.h`).

#pragma once

// Bracket a `state.pause()` region on the registered profiler manager.
// No-ops when none is registered, or it provides no pause()/resume().
void mew_profiler_pause();
void mew_profiler_resume();
