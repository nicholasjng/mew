// Regression tests for the bundled Google Benchmark patches, independent of
// Python's GIL and with real fixtures to exercise teardown ordering.
#include <benchmark/benchmark.h>

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <thread>

namespace {
void Check(bool condition, const char* message) {
    if (!condition) {
        std::cerr << message << '\n';
        std::abort();
    }
}

// Reusable rendezvous of a fixed number of threads.
class Rendezvous {
   public:
    explicit Rendezvous(int count) : count_(count) {}
    void Wait() {
        std::unique_lock<std::mutex> lock(mutex_);
        const int generation = generation_;
        if (++arrived_ == count_) {
            arrived_ = 0;
            ++generation_;
            cv_.notify_all();
            return;
        }
        const bool released =
            cv_.wait_for(lock, std::chrono::seconds(10), [&] { return generation != generation_; });
        Check(released, "rendezvous sized by State::threads() waited for absent workers");
    }

   private:
    std::mutex mutex_;
    std::condition_variable cv_;
    const int count_;
    int arrived_ = 0;
    int generation_ = 0;
};

enum class Body { Normal, Batch, Break, BreakLast, Skip, SkipBefore, NoLoop };
Body body;
int thread_count;
int early_thread;
// Sized in the plain benchmark's Setup() callback from State::threads().
std::unique_ptr<Rendezvous> rendezvous;
std::thread::id main_thread;
std::atomic<bool> active{false};
std::atomic<int> operations{0};
std::array<std::atomic<int>, 4> invocations{};
std::array<std::atomic<int>, 3> setup_done{};
std::array<std::atomic<int>, 3> loop_done{};
std::array<std::atomic<int>, 3> teardown_done{};
int starts;
int stops;

// Pass 0 is the timed run; the memory (1) and profiler (2) passes run thread 0 alone.
int Workers(int pass) { return pass == 0 ? thread_count : 1; }

// Only thread 0 runs the manager passes, so only its early exit can end one.
bool Complete() { return body == Body::Normal || body == Body::Batch || early_thread != 0; }
int collected;
int memory_finalized;
int memory_accepted;

void Start(int pass) {
    Check(std::this_thread::get_id() == main_thread, "start hook left thread 0");
    Check(setup_done[pass] == Workers(pass), "measurement began during setup");
    Check(!active.exchange(true), "duplicate start");
    operations = 0;
    ++starts;
}

int Stop(int pass) {
    Check(std::this_thread::get_id() == main_thread, "stop hook left thread 0");
    Check(loop_done[pass] == Workers(pass), "measurement stopped before the loop exited");
    Check(teardown_done[pass] == 0, "fixture teardown preceded stop hook");
    Check(active.exchange(false), "stop without start");
    ++stops;
    return operations;
}

struct Memory : benchmark::MemoryManager {
    void Start() override { ::Start(1); }
    void Stop(Result& result) override { result.num_allocs = ::Stop(1); }
    void OnPassComplete(bool completed) override {
        Check(std::this_thread::get_id() == main_thread, "completion left thread 0");
        Check(!active && teardown_done[1] == 1, "memory pass finalized before cleanup");
        Check(completed == Complete(), "incorrect memory-pass completion status");
        ++memory_finalized;
        if (completed) ++memory_accepted;
    }
} memory;

struct Profiler : benchmark::ProfilerManager {
    int measured = 0;
    void AfterSetupStartWithState(const benchmark::State& state) override {
        Check(state.thread_index() == 0, "start received another worker's State");
        Start(2);
    }
    void BeforeTeardownStopWithState(const benchmark::State& state) override {
        Check(state.thread_index() == 0, "stop received another worker's State");
        measured = Stop(2);
    }
    void GetResult(Result& result) override {
        Check(teardown_done[2] == 1, "results collected before teardown");
        ++collected;
        result.values["operations"] = measured;
    }
} profiler;

class TestFixture : public benchmark::Fixture {
   public:
    TestFixture() {
        SetName("manager_pass_test");
        Iterations(20);
        Threads(thread_count);
    }
    void SetUp(benchmark::State& state) override {
        const int pass = invocations[state.thread_index()]++;
        Check(state.threads() == Workers(pass), "State::threads() disagrees with the pass");
        Check(!active, "worker setup was measured");
        ++setup_done[pass];
    }
    void TearDown(benchmark::State& state) override {
        const int pass = invocations[state.thread_index()] - 1;
        Check(!active, "fixture teardown was measured");
        ++teardown_done[pass];
    }
    void BenchmarkCase(benchmark::State& state) override {
        RunBody(state);
        // As mew's trampoline does: a body that left its loop, including in the
        // final iteration, is an incomplete measurement.
        if (state.in_timed_section()) {
            state.SkipWithError("The benchmark did not complete its loop.");
        }
    }

    void RunBody(benchmark::State& state) {
        const int pass = invocations[state.thread_index()] - 1;
        // Every thread of the run arrives, including those about to exit early.
        if (rendezvous) rendezvous->Wait();
        const bool early = pass != 0 && state.thread_index() == early_thread;
        if (early && (body == Body::SkipBefore || body == Body::NoLoop)) {
            if (body == Body::SkipBefore) state.SkipWithError("before loop");
            ++loop_done[pass];
            return;
        }
        int count = 0;
        const int batch = body == Body::Batch ? 10 : 1;
        while (state.KeepRunningBatch(batch)) {
            count += batch;
            if (active) operations += batch;
            if (early && (body == Body::Break || body == Body::Skip ||
                          (body == Body::BreakLast && count >= state.max_iterations))) {
                ++loop_done[pass];
                if (body == Body::Skip) state.SkipWithError("inside loop");
                return;
            }
            if (count >= state.max_iterations) ++loop_done[pass];
        }
        Check(!active, "worker post-loop code was measured");
        Check(!state.KeepRunning(), "finished loop yielded more iterations");
    }
};

struct Reporter : benchmark::BenchmarkReporter {
    int rows = 0;
    bool ReportContext(const Context&) override { return true; }
    void ReportRuns(const std::vector<Run>& runs) override {
        for (const auto& run : runs) {
            ++rows;
            Check(run.skipped == benchmark::internal::NotSkipped, "timed run skipped");
            const bool complete = Complete();
            // A batch of 10 overshoots the memory pass's 4 iterations.
            const int expected = body == Body::Batch ? 10 : 4;
            Check(run.memory_result.memory_iterations == (complete ? expected : 0),
                  "incorrect memory iteration count");
            if (complete) {
                Check(run.memory_result.num_allocs == expected, "incorrect allocation count");
                Check(run.allocs_per_iter == 1.0, "incorrect allocation rate");
                Check(run.profile_result.values.at("operations") == 20,
                      "incorrect profiled operation count");
            } else {
                Check(run.profile_result.values.empty(), "partial profile was reported");
            }
        }
    }
};
}  // namespace

int main(int argc, char** argv) {
    benchmark::Initialize(&argc, argv);
    main_thread = std::this_thread::get_id();
    benchmark::RegisterMemoryManager(&memory);
    benchmark::RegisterProfilerManager(&profiler);
    int scenarios = 0;
    for (int threads : {1, 2, 4}) {
        thread_count = threads;
        for (Body test_body : {Body::Normal, Body::Batch, Body::Break, Body::BreakLast, Body::Skip,
                               Body::SkipBefore, Body::NoLoop}) {
            body = test_body;
            for (int variant = 0; variant != 4; ++variant) {
                const int exiting_thread = variant % 2 == 0 ? 0 : threads - 1;
                const bool use_fixture = variant < 2;
                early_thread = exiting_thread;
                for (auto& value : invocations) value = 0;
                for (auto& value : setup_done) value = 0;
                for (auto& value : loop_done) value = 0;
                for (auto& value : teardown_done) value = 0;
                starts = stops = collected = memory_finalized = memory_accepted = 0;
                TestFixture plain_body;
                if (use_fixture) {
                    benchmark::internal::RegisterBenchmarkInternal(std::make_unique<TestFixture>());
                } else {
                    benchmark::RegisterBenchmark("plain_manager_pass",
                                                 [&](benchmark::State& state) {
                                                     plain_body.SetUp(state);
                                                     plain_body.BenchmarkCase(state);
                                                 })
                        ->Iterations(20)
                        ->Threads(thread_count)
                        ->Setup([](const benchmark::State& state) {
                            rendezvous = std::make_unique<Rendezvous>(state.threads());
                        })
                        ->Teardown([](const benchmark::State& state) {
                            Check(!active, "plain benchmark teardown was measured");
                            const int pass = invocations[0] - 1;
                            Check(state.threads() == Workers(pass),
                                  "Teardown() State::threads() disagrees with the pass");
                            teardown_done[pass] = Workers(pass);
                            rendezvous.reset();
                        });
                }
                Reporter reporter;
                benchmark::RunSpecifiedBenchmarks(&reporter);
                const bool complete = Complete();
                const bool no_start =
                    early_thread == 0 && (body == Body::SkipBefore || body == Body::NoLoop);
                Check(reporter.rows == 1, "missing report");
                Check(starts == (no_start ? 0 : 2) && stops == starts, "unpaired hooks");
                Check(collected == (complete ? 1 : 0), "partial pass collected results");
                Check(memory_finalized == 1 && memory_accepted == (complete ? 1 : 0),
                      "memory pass was not finalized exactly once");
                for (int pass = 0; pass != 3; ++pass)
                    Check(teardown_done[pass] == Workers(pass), "fixture teardown did not finish");
                benchmark::ClearRegisteredBenchmarks();
                ++scenarios;
            }
        }
    }
    benchmark::RegisterMemoryManager(nullptr);
    benchmark::RegisterProfilerManager(nullptr);
    benchmark::Shutdown();
    std::cout << scenarios << " manager-pass scenarios passed\n";
}
