// Profiler hooks used by State.pause().

#pragma once

// Bracket a `state.pause()` region on the registered profiler manager.
// No-ops when none is registered, or it provides no pause()/resume().
void mew_profiler_pause();
void mew_profiler_resume();
