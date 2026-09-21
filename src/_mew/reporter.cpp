// Python adapter for Google Benchmark's reporter interface.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <exception>
#include <memory>
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

// Plain strings, not the bound enums: a serialized row would carry "TimeUnit.ns".
const char* time_unit_name(benchmark::TimeUnit u) {
    switch (u) {
        case benchmark::kNanosecond:
            return "ns";
        case benchmark::kMicrosecond:
            return "us";
        case benchmark::kMillisecond:
            return "ms";
        case benchmark::kSecond:
            return "s";
    }
    return "ns";
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

nb::dict profile_block(const Run& r) {
    nb::dict d;
    for (const auto& kv : r.profile_result.labels) d[kv.first.c_str()] = kv.second;
    for (const auto& kv : r.profile_result.values) d[kv.first.c_str()] = kv.second;
    return d;
}

// Convert a native result to the public mapping shape.
// GB's decomposed name, so readers need not parse `/min_time:…` suffixes back
// out of `name`.
nb::dict name_parts(const benchmark::BenchmarkName& n) {
    nb::dict d;
    d["function_name"] = n.function_name;
    d["args"] = n.args;
    d["min_time"] = n.min_time;
    d["min_warmup_time"] = n.min_warmup_time;
    d["iterations"] = n.iterations;
    d["repetitions"] = n.repetitions;
    d["time_type"] = n.time_type;
    d["threads"] = n.threads;
    return d;
}

nb::dict run_to_dict(const Run& r) {
    nb::dict d;
    d["name"] = r.benchmark_name();
    d["run_name"] = r.run_name.str();
    d["name_parts"] = name_parts(r.run_name);
    d["family_index"] = r.family_index;
    d["per_family_instance_index"] = r.per_family_instance_index;
    d["run_type"] = r.run_type == Run::RT_Aggregate ? "aggregate" : "iteration";
    d["aggregate_name"] = r.aggregate_name;
    d["repetitions"] = r.repetitions;
    d["repetition_index"] = r.repetition_index;
    d["threads"] = r.threads;
    d["iterations"] = r.iterations;
    d["real_time"] = r.GetAdjustedRealTime();
    d["cpu_time"] = r.GetAdjustedCPUTime();
    d["real_accumulated_time"] = r.real_accumulated_time;
    d["cpu_accumulated_time"] = r.cpu_accumulated_time;
    d["time_unit"] = time_unit_name(r.time_unit);
    d["label"] = r.report_label;
    d["skipped"] = r.skipped != benchmark::internal::NotSkipped;
    d["skip_message"] = r.skip_message;
    nb::dict counters;
    for (const auto& kv : r.counters) counters[kv.first.c_str()] = kv.second.value;
    d["counters"] = counters;
    // Both blocks ride on the Run: GB stamps the memory result, mew's patch the
    // profiler one. Neither needs a lookup.
    if (r.memory_result.memory_iterations > 0) d["memory"] = memory_block(r);
    if (!r.profile_result.values.empty() || !r.profile_result.labels.empty())
        d["cpu_profile"] = profile_block(r);
    return d;
}

class PyReporter : public BenchmarkReporter {
   public:
    nb::object py;
    // Context supplied by the Python runner.
    nb::dict extra_context;
    // Rows for benchmarks rejected before native registration.
    nb::list extra_rows;

    PyReporter(nb::object obj, nb::dict extra, nb::list rows)
        : py(std::move(obj)), extra_context(std::move(extra)), extra_rows(std::move(rows)) {}

    ~PyReporter() override {
        nb::gil_scoped_acquire gil;
        py.reset();
        extra_context.reset();
        extra_rows.reset();
    }

    bool ReportContext(const Context&) override {
        nb::gil_scoped_acquire gil;
        try {
            py.attr("report_context")(extra_context);
            if (extra_rows.size() > 0) py.attr("report_runs")(extra_rows);
            return true;
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return false;
        }
    }

    void ReportRuns(const std::vector<Run>& runs) override {
        nb::gil_scoped_acquire gil;
        try {
            nb::list rows;
            for (const auto& r : runs) {
                rows.append(run_to_dict(r));
            }
            py.attr("report_runs")(rows);
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
        }
    }

    void Finalize() override {
        nb::gil_scoped_acquire gil;
        try {
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
        "preload_system_info",
        [] {
            nb::gil_scoped_release release;
            benchmark::CPUInfo::Get();
            benchmark::SystemInfo::Get();
        },
        "Initialize Google Benchmark's CPU and system information.");
    m.def(
        "cpu_info",
        [] {
            const auto& cpu = benchmark::CPUInfo::Get();
            nb::dict d;
            d["num_cpus"] = cpu.num_cpus;
            const char* scaling = "unknown";
            if (cpu.scaling == benchmark::CPUInfo::ENABLED)
                scaling = "enabled";
            else if (cpu.scaling == benchmark::CPUInfo::DISABLED)
                scaling = "disabled";
            d["cpu_scaling"] = scaling;
            return d;
        },
        "CPU count and frequency-scaling state.\n"
        "Scaling probes sysfs on Linux and sysctl on macOS. `\"unknown\"` when undetectable.");

    nb::enum_<benchmark::TimeUnit>(m, "TimeUnit", nb::is_str(),
                                   "Time unit used for reported per-iteration durations.")
        .str_value("ns", benchmark::kNanosecond, "ns")
        .str_value("us", benchmark::kMicrosecond, "us")
        .str_value("ms", benchmark::kMillisecond, "ms")
        .str_value("s", benchmark::kSecond, "s");

    m.def(
        "run_benchmarks",
        [](std::vector<std::string> argv, nb::object reporter, nb::dict extra_context,
           nb::list extra_rows) {
            // GB only shuffles the char** array, never writes into the strings.
            if (argv.empty()) argv.emplace_back("mew");
            std::vector<char*> argp;
            argp.reserve(argv.size());
            for (auto& s : argv) argp.push_back(s.data());

            int argc = (int)argp.size();
            // Re-parse flags every call so a different argv per call takes effect.
            // `--help` in argv still triggers exit(0); documented GB behavior.
            benchmark::Initialize(&argc, argp.data());

            std::unique_ptr<PyReporter> pr;
            if (!reporter.is_none()) {
                pr = std::make_unique<PyReporter>(reporter, extra_context, extra_rows);
            }

            size_t count;
            {
                nb::gil_scoped_release release;
                count = pr ? benchmark::RunSpecifiedBenchmarks(pr.get())
                           : benchmark::RunSpecifiedBenchmarks();
            }

            // Do NOT clear here: callers clear before registering and atexit
            // handles teardown, so BenchmarkHandles stay valid until the next clear.

            if (auto abort = mew_take_pending_abort()) {
                std::rethrow_exception(abort);
            }
            return count;
        },
        "argv"_a, "reporter"_a = nb::none(), "extra_context"_a = nb::dict(),
        "extra_rows"_a = nb::list(),
        "Initialize Google Benchmark with `argv` and run all registered benchmarks.\n"
        "Returns the number of benchmarks run.");
}
