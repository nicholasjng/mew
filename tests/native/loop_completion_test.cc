// A last-iteration return must report an error, including when already paused.
#include <benchmark/benchmark.h>

#include <cstdlib>
#include <iostream>

namespace {
void IncompleteLoop(benchmark::State& state) {
    while (state.range(0) == 1 ? state.KeepRunningBatch(7) : state.KeepRunning()) {
        if (state.thread_index() == state.threads() - 1 &&
            state.iterations() >= state.max_iterations) {
            if (state.range(0) == 2) state.PauseTiming();
            return;
        }
    }
}
BENCHMARK(IncompleteLoop)->Arg(0)->Arg(1)->Arg(2)->Iterations(1)->ThreadRange(1, 4);

struct Reporter : benchmark::BenchmarkReporter {
    int rows = 0;
    bool ReportContext(const Context&) override { return true; }
    void ReportRuns(const std::vector<Run>& runs) override {
        for (const auto& run : runs) {
            ++rows;
            if (run.skipped != benchmark::internal::SkippedWithError ||
                run.skip_message != "The benchmark did not complete its loop.") {
                std::cerr << "incomplete loop reported as a successful measurement\n";
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
    return reporter.rows == 9 ? 0 : 1;
}
