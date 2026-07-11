// Reporter bindings: exposes Run/Context as Python objects, bridges a Python
// reporter into GB's BenchmarkReporter interface, and exposes `run_benchmarks`.

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <map>

#include <exception>
#include <memory>
#include <string>
#include <vector>

#include "interrupt.h"
#include "managers.h"

namespace nb = nanobind;
using namespace nb::literals;

using benchmark::BenchmarkReporter;
using Run = BenchmarkReporter::Run;
using Context = BenchmarkReporter::Context;

namespace {

nb::dict build_context_dict(const Context& ctx) {
    const auto& cpu = ctx.cpu_info;
    const auto& sys = ctx.sys_info;
    nb::dict d;
    d["num_cpus"] = cpu.num_cpus;
    d["mhz_per_cpu"] = cpu.cycles_per_second / 1e6;
    const char* scaling = "unknown";
    if (cpu.scaling == benchmark::CPUInfo::ENABLED)
        scaling = "enabled";
    else if (cpu.scaling == benchmark::CPUInfo::DISABLED)
        scaling = "disabled";
    d["cpu_scaling"] = scaling;
    d["library_build_type"] =
#ifdef NDEBUG
        "release";
#else
        "debug";
#endif
    d["host_name"] = sys.name;
    d["executable"] =
        Context::executable_name ? std::string(Context::executable_name) : std::string();
    return d;
}

// RunRow carries plain strings, not the bound enums: the row is serialized to
// JSON/Parquet, where an enum would land as "TimeUnit.ns".
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
    // Tombstones mean the manager did not report the figure; omit rather than
    // serialize INT64_MAX, as GB's own JSON reporter does.
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

// The single Run -> RunRow projection. Everything a reporter sees comes through
// here.
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
    // Both blocks ride on the Run itself: the memory manager's result is stamped
    // by GB, the profiler manager's by mew's patch. Neither needs a lookup.
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
    // Rows mew built itself (benchmarks it declined to run), flushed right after
    // the context so they land before finalize, where buffering reporters write.
    nb::list extra_rows;
    // GB's reporter interface is noexcept; stash callback exceptions here and
    // rethrow from `run_benchmarks` after the loop returns.
    std::exception_ptr pending_exception;

    PyReporter(nb::object obj, nb::dict extra, nb::list rows)
        : py(std::move(obj)), extra_context(std::move(extra)), extra_rows(std::move(rows)) {}

    ~PyReporter() override {
        nb::gil_scoped_acquire gil;
        py.reset();
        extra_context.reset();
        extra_rows.reset();
    }

    bool ReportContext(const Context& ctx) override {
        nb::gil_scoped_acquire gil;
        try {
            nb::dict ctx_dict = build_context_dict(ctx);
            // Overlay caller keys last so provenance wins over GB defaults.
            for (auto [k, v] : extra_context) ctx_dict[k] = v;
            auto res = py.attr("report_context")(ctx_dict);
            bool ok = res.is_none() ? true : nb::cast<bool>(res);
            // A reporter that vetoed the session must not receive rows.
            if (ok && extra_rows.size() > 0) py.attr("report_runs")(extra_rows);
            return ok;
        } catch (...) {
            if (!pending_exception) pending_exception = std::current_exception();
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
            if (!pending_exception) pending_exception = std::current_exception();
        }
    }

    void Finalize() override {
        nb::gil_scoped_acquire gil;
        try {
            if (nb::hasattr(py, "finalize")) py.attr("finalize")();
        } catch (...) {
            if (!pending_exception) pending_exception = std::current_exception();
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
    nb::enum_<benchmark::TimeUnit>(m, "TimeUnit", nb::is_str(),
                                   "Time unit used for reported per-iteration durations.")
        .str_value("ns", benchmark::kNanosecond, "ns")
        .str_value("us", benchmark::kMicrosecond, "us")
        .str_value("ms", benchmark::kMillisecond, "ms")
        .str_value("s", benchmark::kSecond, "s");

    nb::enum_<Run::RunType>(m, "RunType", nb::is_str(),
                            "Distinguishes per-repetition runs from aggregate "
                            "(mean / median / stddev) rows.")
        .str_value("iteration", Run::RT_Iteration, "iteration")
        .str_value("aggregate", Run::RT_Aggregate, "aggregate");

    nb::class_<benchmark::BenchmarkName>(m, "BenchmarkName",
                                         "A registered name split into its parts.\n"
                                         "Google Benchmark assembles these into the reported "
                                         "`function/args/min_time:...` string; `str()` renders it.")
        .def_ro("function_name", &benchmark::BenchmarkName::function_name)
        .def_ro("args", &benchmark::BenchmarkName::args)
        .def_ro("min_time", &benchmark::BenchmarkName::min_time)
        .def_ro("min_warmup_time", &benchmark::BenchmarkName::min_warmup_time)
        .def_ro("iterations", &benchmark::BenchmarkName::iterations)
        .def_ro("repetitions", &benchmark::BenchmarkName::repetitions)
        .def_ro("time_type", &benchmark::BenchmarkName::time_type)
        .def_ro("threads", &benchmark::BenchmarkName::threads)
        .def("__str__", &benchmark::BenchmarkName::str);

    nb::class_<Run>(m, "Run",
                    "A single benchmark run report.\n"
                    "Times are in seconds (accumulated across iterations); use "
                    "`adjusted_real_time()` for per-iteration averages.\n"
                    "`to_dict()` projects it to a `RunRow`; that is what reporters "
                    "receive.")
        .def_ro("run_name", &Run::run_name)
        .def("benchmark_name", &Run::benchmark_name)
        .def_ro("family_index", &Run::family_index)
        .def_ro("per_family_instance_index", &Run::per_family_instance_index)
        .def_ro("run_type", &Run::run_type)
        .def_ro("aggregate_name", &Run::aggregate_name)
        .def_ro("report_label", &Run::report_label)
        .def_ro("skip_message", &Run::skip_message)
        .def_ro("iterations", &Run::iterations)
        .def_ro("threads", &Run::threads)
        .def_ro("repetition_index", &Run::repetition_index)
        .def_ro("repetitions", &Run::repetitions)
        .def_ro("time_unit", &Run::time_unit)
        .def_ro("real_accumulated_time", &Run::real_accumulated_time)
        .def_ro("cpu_accumulated_time", &Run::cpu_accumulated_time)
        .def("adjusted_real_time", &Run::GetAdjustedRealTime)
        .def("adjusted_cpu_time", &Run::GetAdjustedCPUTime)
        .def_ro("complexity_n", &Run::complexity_n)
        .def_prop_ro("counters",
                     [](const Run& r) {
                         nb::dict d;
                         for (const auto& kv : r.counters) {
                             d[kv.first.c_str()] = kv.second.value;
                         }
                         return d;
                     })
        .def_prop_ro("skipped",
                     [](const Run& r) { return r.skipped != benchmark::internal::NotSkipped; })
        .def(
            "to_dict",
            [](const Run& r, const nb::kwargs& extra) {
                nb::dict d = run_to_dict(r);
                // Merged last, so an overlay wins over a base key.
                for (auto [k, v] : extra) d[k] = v;
                return d;
            },
            "kwargs"_a,
            "Project to a `RunRow` dict.\n"
            "Keyword arguments are merged last, so an overlay wins over a base key.\n"
            "A `memory` block is present when a memory manager ran, a `cpu_profile` "
            "block when a profiler manager reported a result.");

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

            // Defensive: drop any interrupt a previous run failed to consume so
            // it can't abort this run's first benchmark.
            mew_take_pending_interrupt();

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

            // A KeyboardInterrupt/SystemExit from a benchmark body outranks any
            // reporter-callback exception: it is the user's stop request.
            if (auto interrupt = mew_take_pending_interrupt()) {
                std::rethrow_exception(interrupt);
            }
            // Rethrow the first reporter-callback exception; nanobind's trampoline
            // restores the Python error indicator from the captured `python_error`.
            if (pr && pr->pending_exception) {
                std::rethrow_exception(pr->pending_exception);
            }
            // Manager callbacks (memory capture, profiler summary) rank below a
            // reporter failure: a reporter that raised means no results landed.
            if (auto manager_exc = mew_take_pending_manager_exception()) {
                std::rethrow_exception(manager_exc);
            }
            return count;
        },
        "argv"_a, "reporter"_a = nb::none(), "extra_context"_a = nb::dict(),
        "extra_rows"_a = nb::list(),
        "Initialize Google Benchmark with `argv` and run all registered benchmarks.\n"
        "Returns the number of benchmarks run.\n"
        "`extra_context` keys are overlaid onto the context dict passed to the "
        "reporter's `report_context` (session id/tag, user context).\n"
        "`extra_rows` are pre-built RunRows reported right after the context, for "
        "benchmarks mew declined to run.\n"
        "Pass a `Fanout` reporter to multiplex into multiple sinks.");
}
