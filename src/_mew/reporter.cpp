// Python adapter for Google Benchmark's reporter interface.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <cstdio>
#include <exception>
#include <string>
#include <thread>
#include <vector>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

using benchmark::BenchmarkReporter;
using Run = BenchmarkReporter::Run;
using Context = BenchmarkReporter::Context;

namespace {

// Per-iteration time in nanoseconds, from GB's accumulated seconds: rows hold
// one unit, whatever unit GB would have reported in.
double per_iteration_ns(double accumulated_seconds, benchmark::IterationCount iterations) {
    double ns = accumulated_seconds * 1e9;
    return iterations != 0 ? ns / static_cast<double>(iterations) : ns;
}

nb::dict memory_block(const Run& r) {
    const auto& mem = r.memory_result;
    nb::dict d;
    d["peak_bytes"] = mem.max_bytes_used;
    d["total_allocations"] = mem.num_allocs;
    d["iterations"] = mem.memory_iterations;
    d["allocations_per_iteration"] = r.allocs_per_iter;
    // Tombstone = not reported; omit rather than serialize INT64_MAX, as GB does.
    if (mem.total_allocated_bytes != benchmark::MemoryManager::TombstoneValue)
        d["total_bytes"] = mem.total_allocated_bytes;
    if (mem.net_heap_growth != benchmark::MemoryManager::TombstoneValue)
        d["net_heap_growth"] = mem.net_heap_growth;
    return d;
}

// The registered name (`name[label]` for a family case) plus `/threads:N`,
// without the case index argument.
std::string addressable_name(const Run& r) {
    std::string name = r.run_name.function_name;
    if (!r.run_name.threads.empty()) name += "/" + r.run_name.threads;
    return name;
}

// `params` maps a registered name (`name[label]` for a family case) to its
// case's parameters, already encoded for JSON by the runner.
nb::dict run_to_dict(const Run& r, const nb::dict& params) {
    nb::dict d;
    d["name"] = addressable_name(r);
    nb::str registered(r.run_name.function_name.c_str());
    // A copy per row: a reporter that edits one row's params must not edit the next.
    d["params"] = params.contains(registered) ? params[registered].attr("copy")() : nb::dict();
    d["repetition"] = r.repetition_index;
    d["repetitions"] = r.repetitions;
    d["threads"] = r.threads;
    d["iterations"] = r.iterations;
    d["real_time"] = per_iteration_ns(r.real_accumulated_time, r.iterations);
    d["cpu_time"] = per_iteration_ns(r.cpu_accumulated_time, r.iterations);
    d["label"] = r.report_label;
    if (r.skipped == benchmark::internal::NotSkipped) {
        d["skip"] = nb::none();
    } else {
        nb::dict skip;
        skip["reason"] = r.skipped == benchmark::internal::SkippedWithError ? "error" : "skipped";
        skip["message"] = r.skip_message;
        d["skip"] = skip;
    }
    // A list of records, not a name -> value map: readers like DuckDB infer one
    // type for it whichever counters a file contains.
    nb::list counters;
    for (const auto& [name, c] : r.counters) {
        nb::dict counter;
        counter["name"] = name;
        counter["value"] = c.value;
        // Display hints, only for counters that aren't plain decimal numbers.
        nb::dict format;
        if (c.oneK == benchmark::Counter::kIs1024) format["one_k"] = 1024;
        if (c.flags & benchmark::Counter::kIsRate) {
            format["rate"] = true;
            if (c.flags & benchmark::Counter::kInvert) format["inverted"] = true;
        }
        if (c.flags & benchmark::Counter::kStringifyExponent) format["exponent"] = true;
        if (format.size() > 0) counter["format"] = format;
        counters.append(counter);
    }
    d["counters"] = counters;
    // GB stamps the memory result on the Run, mew's patch the profiler one.
    // A memory manager that measured nothing leaves its tombstone in num_allocs.
    if (r.memory_result.memory_iterations > 0 &&
        r.memory_result.num_allocs != benchmark::MemoryManager::TombstoneValue)
        d["memory"] = memory_block(r);
    if (!r.profile_result.json.empty())
        d["cpu_profile"] = nb::module_::import_("json").attr("loads")(r.profile_result.json);
    return d;
}

class PyReporter : public BenchmarkReporter {
   public:
    nb::object py;
    // mew's run and provenance block, reported instead of GB's Context.
    nb::dict run_context;
    // Registered name -> the case's parameters; see run_to_dict.
    nb::dict params;
    // mew.runner.OutputCapture, or None. Suspended around every callback so
    // reporters write to the real streams.
    nb::object capture;

    // Constructed and destroyed by `run_benchmarks` while it holds the GIL.
    PyReporter(nb::object obj, nb::dict context, nb::dict case_params, nb::object cap)
        : py(std::move(obj)),
          run_context(std::move(context)),
          params(std::move(case_params)),
          capture(std::move(cap)) {}

    // Both flush C stdio first, so output buffered while redirected stays on its side.
    void suspend_capture() {
        if (capture.is_none()) return;
        std::fflush(nullptr);
        capture.attr("suspend")();
    }
    void resume_capture() {
        if (capture.is_none()) return;
        std::fflush(nullptr);
        capture.attr("resume")();
    }

    bool ReportContext(const Context&) override {
        nb::gil_scoped_acquire gil;
        try {
            suspend_capture();
            py.attr("report_context")(run_context);
            resume_capture();
            return true;
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return false;
        }
    }

    void ReportRuns(const std::vector<Run>& runs) override {
        // The run is being torn down: later benchmarks only report placeholder
        // "aborted" rows, which must not reach result files.
        if (mew_abort_pending()) return;
        nb::gil_scoped_acquire gil;
        try {
            suspend_capture();
            nb::list rows;
            for (const auto& r : runs) {
                // mew reduces repetitions itself, so drop GB-side aggregates.
                if (r.run_type == Run::RT_Aggregate) continue;
                rows.append(run_to_dict(r, params));
            }
            // Google Benchmark reports a benchmark's aggregates as a batch of their own.
            if (rows.size() > 0) py.attr("report_results")(rows);
            resume_capture();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
        }
    }

    void Finalize() override {
        nb::gil_scoped_acquire gil;
        try {
            // Stays suspended: the run is over, and the caller closes the capture.
            suspend_capture();
            if (nb::hasattr(py, "finalize")) py.attr("finalize")();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
        }
    }
};

}  // namespace

void register_reporter(nb::module_& m) {
    m.def(
        "warmup_free_threading",
        [] {
            // Attach one thread before Google Benchmark starts its workers.
            nb::gil_scoped_release release;
            std::thread worker([] { nb::gil_scoped_acquire acquire; });
            worker.join();
        },
        "Attach one native thread to initialize free-threaded CPython state.");
    m.def(
        "cpu_info",
        [] {
            const auto& cpu = benchmark::CPUInfo::Get();
            nb::dict d;
            d["num_cpus"] = cpu.num_cpus;
            d["cpu_scaling_enabled"] = cpu.scaling == benchmark::CPUInfo::ENABLED;
            return d;
        },
        "CPU count, and whether frequency scaling was detected (sysfs on Linux, sysctl on macOS).");

    m.def(
        "run_benchmarks",
        [](std::vector<std::string> argv, nb::object reporter, nb::dict run_context,
           nb::dict params, nb::object capture) {
            // GB only shuffles the char** array, never writes into the strings.
            std::vector<char*> argp;
            argp.reserve(argv.size());
            for (auto& s : argv) argp.push_back(s.data());

            int argc = (int)argp.size();
            // Re-parse flags every call so a different argv per call takes effect.
            // `--help` in argv still triggers exit(0); documented GB behavior.
            benchmark::Initialize(&argc, argp.data());

            PyReporter pr(std::move(reporter), std::move(run_context), std::move(params),
                          std::move(capture));
            size_t count;
            {
                nb::gil_scoped_release release;
                count = benchmark::RunSpecifiedBenchmarks(&pr);
            }

            // Not cleared here: callers clear before registering, and atexit at
            // teardown, which keeps the registered callables alive until then.

            if (auto abort = mew_take_pending_abort()) {
                std::rethrow_exception(abort);
            }
            return count;
        },
        "argv"_a, "reporter"_a, "run_context"_a = nb::dict(), "params"_a = nb::dict(),
        "capture"_a = nb::none(),
        "Initialize Google Benchmark with `argv` and run all registered benchmarks,\n"
        "reporting through `reporter`.\n"
        "`params` maps a registered name to its case's parameters, stamped on its rows.\n"

        "`capture` (an active mew.runner.OutputCapture) is suspended around every\n"
        "reporter callback, so reporters write to the real stdout/stderr.\n"
        "Returns the number of benchmarks run.");
}
