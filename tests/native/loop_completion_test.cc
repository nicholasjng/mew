// A loop that runs to completion inside a paused region is a valid measurement.
#include <benchmark/benchmark.h>

#include <cstdlib>
#include <iostream>

namespace {
void PausedAtLoopEnd(benchmark::State& state) {
    for (auto _ : state) {
        if (state.timer_running()) state.PauseTiming();
    }
    if (state.timer_running()) {
        std::cerr << "timer running after a loop that ended paused\n";
        std::abort();
    }
}
BENCHMARK(PausedAtLoopEnd)
    ->Iterations(3)
    ->ThreadRange(1, 4)
    // Setup and teardown States have no timer.
    ->Setup([](const benchmark::State& state) {
        if (state.timer_running()) std::abort();
    });

struct Reporter : benchmark::BenchmarkReporter {
    int rows = 0;
    bool ReportContext(const Context&) override { return true; }
    void ReportRuns(const std::vector<Run>& runs) override {
        for (const auto& run : runs) {
            ++rows;
            if (run.skipped != benchmark::internal::NotSkipped) {
                std::cerr << "a loop that ended paused was rejected\n";
                std::abort();
            }
        }
    }
};
}  // namespace

int main(int argc, char** argv) {
    benchmark::Initialize(&argc, argv);
    Reporter reporter;
    benchmark::RunSpecifiedBenchmarks(&reporter);
    benchmark::Shutdown();
    return reporter.rows == 3 ? 0 : 1;
}
