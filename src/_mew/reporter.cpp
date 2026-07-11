// Reporter bindings: bridges a Python reporter into GB's BenchmarkReporter
// interface and exposes `run_benchmarks`.
//
// The C++ `Run` is never handed to Python: `run_to_dict` projects it to a
// `BenchmarkResult` at the boundary, so reporters only ever see dicts -- the one shape
// every row source can produce, including the ones Google Benchmark never made.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <exception>
#include <memory>
#include <string>
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

// The single Run -> BenchmarkResult projection; everything a reporter sees comes through here.
nb::dict run_to_dict(const Run& r) {
    nb::dict d;
    d["name"] = r.benchmark_name();
    d["run_name"] = r.run_name.str();
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
    // Caller keys (session id/tag, user context) overlaid onto the GB context
    // before the Python reporter sees it. Empty when no provenance is passed.
    nb::dict extra_context;
    // Rows mew built itself (benchmarks it declined to run), flushed after the
    // context so they precede finalize, where buffering reporters write.
    nb::list extra_rows;

    PyReporter(nb::object obj, nb::dict extra, nb::list rows)
        : py(std::move(obj)), extra_context(std::move(extra)), extra_rows(std::move(rows)) {}

    ~PyReporter() override {
        nb::gil_scoped_acquire gil;
        py.reset();
        extra_context.reset();
        extra_rows.reset();
    }

    // GB's own Context carries nothing mew needs: `mew.runner` assembles the
    // whole block and passes it as `extra_context`.
    bool ReportContext(const Context&) override {
        nb::gil_scoped_acquire gil;
        try {
            py.attr("report_context")(extra_context);
            if (extra_rows.size() > 0) py.attr("report_runs")(extra_rows);
            return true;
        } catch (...) {
            // The only way a reporter stops the run: `false` makes GB skip every
            // benchmark, and `run_benchmarks` rethrows once the loop returns.
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
        "preload_system_info",
        [] {
            nb::gil_scoped_release release;
            benchmark::CPUInfo::Get();
            benchmark::SystemInfo::Get();
        },
        "Force Google Benchmark's lazy CPU/system-info probes to run now.\n"
        "Their platform diagnostics go straight to fd 2 (e.g. the macOS\n"
        "hw.cpufrequency sysctl failure); calling this under a scoped fd-2\n"
        "redirect keeps that noise out of user-visible stderr without\n"
        "silencing the benchmark run itself.");
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

            // First abort wins, and it already stopped the run, so there is
            // nothing to rank. nanobind restores the Python error indicator.
            if (auto abort = mew_take_pending_abort()) {
                std::rethrow_exception(abort);
            }
            return count;
        },
        "argv"_a, "reporter"_a = nb::none(), "extra_context"_a = nb::dict(),
        "extra_rows"_a = nb::list(),
        "Initialize Google Benchmark with `argv` and run all registered benchmarks.\n"
        "Returns the number of benchmarks run.\n"
        "`extra_context` keys are overlaid onto the context dict passed to the "
        "reporter's `report_context` (session id/tag, user context).\n"
        "`extra_rows` are pre-built BenchmarkResults reported right after the context, for "
        "benchmarks mew declined to run.\n"
        "Pass a `Fanout` reporter to multiplex into multiple sinks.");
}
