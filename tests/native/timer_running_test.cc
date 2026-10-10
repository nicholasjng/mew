// State::timer_running() tracks PauseTiming() and ResumeTiming(); mew's nested
// pause scopes rely on it to leave a stopped timer alone. A loop may also end
// while paused, as when an ExitStack holds a pause scope open over its end.
#include <benchmark/benchmark.h>

#include <cstdlib>
#include <iostream>

namespace {
void Check(bool condition, const char* message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::abort();
    }
}

void PauseAndResume(benchmark::State& state) {
    for (auto _ : state) {
        Check(state.timer_running(), "timer stopped inside the loop");
        state.PauseTiming();
        Check(!state.timer_running(), "timer running after PauseTiming()");
        state.ResumeTiming();
    }
}
BENCHMARK(PauseAndResume)
    ->Iterations(3)
    ->ThreadRange(1, 4)
    // Setup and teardown States have no timer.
    ->Setup([](const benchmark::State& state) {
        Check(!state.timer_running(), "Setup() State has a running timer");
    })
    ->Teardown([](const benchmark::State& state) {
        Check(!state.timer_running(), "Teardown() State has a running timer");
    });

void PausedAtLoopEnd(benchmark::State& state) {
    for (auto _ : state) {
        if (state.timer_running()) state.PauseTiming();
    }
    Check(!state.timer_running(), "timer running after a loop that ended paused");
}
BENCHMARK(PausedAtLoopEnd)->Iterations(3)->ThreadRange(1, 4);

struct Reporter : benchmark::BenchmarkReporter {
    int rows = 0;
    bool ReportContext(const Context&) override { return true; }
    void ReportRuns(const std::vector<Run>& runs) override {
        for (const auto& run : runs) {
            ++rows;
            Check(run.skipped == benchmark::internal::NotSkipped, "run was skipped");
        }
    }
};
}  // namespace

int main(int argc, char** argv) {
    benchmark::Initialize(&argc, argv);
    Reporter reporter;
    benchmark::RunSpecifiedBenchmarks(&reporter);
    benchmark::Shutdown();
    return reporter.rows == 6 ? 0 : 1;
}
