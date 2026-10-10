// Python adapters for Google Benchmark's process-global managers.

#include "managers.h"

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <atomic>
#include <exception>
#include <memory>
#include <string>
#include <utility>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {

void abort_with_type_error(const char* message) {
    mew_set_pending_abort(std::make_exception_ptr(nb::type_error(message)));
}

// Calls the Python manager without unwinding through Google Benchmark: a failing
// hook aborts the run instead. The caller holds the GIL.
class PyManager {
   public:
    explicit PyManager(nb::object obj) : py_(std::move(obj)) {}
    virtual ~PyManager() {
        // Static destruction after Py_Finalize: leak rather than touch the interpreter.
        if (!Py_IsInitialized()) {
            py_.release();
            return;
        }
        nb::gil_scoped_acquire gil;
        py_.reset();
    }

   protected:
    template <typename... Args>
    nb::object call(const char* name, Args&&... args) {
        try {
            return py_.attr(name)(std::forward<Args>(args)...);
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return nb::none();
        }
    }

    nb::object py_;
};

class PyMemoryManager final : public benchmark::MemoryManager, public PyManager {
   public:
    using PyManager::PyManager;

    void Start() override {
        nb::gil_scoped_acquire gil;
        call("start");
    }

    void Stop(Result& out) override {
        nb::gil_scoped_acquire gil;
        nb::object r = call("stop");
        if (r.is_none()) return;
        // A cast_error must not unwind through Google Benchmark.
        try {
            auto metrics = nb::cast<nb::dict>(r);
            // Keys the manager omits keep their tombstone and are dropped by `Run.to_dict`.
            auto take = [&](const char* key, int64_t& target) {
                if (metrics.contains(key)) target = nb::cast<int64_t>(metrics[key]);
            };
            take("total_allocations", out.num_allocs);
            take("peak_bytes", out.max_bytes_used);
            take("total_bytes", out.total_allocated_bytes);
            take("net_heap_growth", out.net_heap_growth);
        } catch (const nb::cast_error&) {
            abort_with_type_error(
                "memory manager stop() must return a dict with integer values, or None");
        }
    }
};

class PyProfilerManager final : public benchmark::ProfilerManager, public PyManager {
   public:
    using PyManager::PyManager;

    void AfterSetupStart() override {
        nb::gil_scoped_acquire gil;
        active_ = true;
        call("after_setup_start");
    }
    void BeforeTeardownStop() override {
        nb::gil_scoped_acquire gil;
        active_ = false;
        call("before_teardown_stop");
    }

    // Only forward pauses during the profiler pass, where the timer is
    // already stopped.
    void Pause() {
        if (!active_) return;
        nb::gil_scoped_acquire gil;
        if (nb::hasattr(py_, "pause")) call("pause");
    }
    void Resume() {
        if (!active_) return;
        nb::gil_scoped_acquire gil;
        if (nb::hasattr(py_, "resume")) call("resume");
    }

    void GetResult(Result& out) override {
        nb::gil_scoped_acquire gil;
        if (!nb::hasattr(py_, "get_result")) return;
        nb::object r = call("get_result");
        if (r.is_none()) return;
        // A cast_error must not unwind through Google Benchmark.
        try {
            // Split into Google Benchmark's label and value maps.
            for (auto [k, v] : nb::cast<nb::dict>(r)) {
                auto key = nb::cast<std::string>(k);
                if (nb::isinstance<nb::str>(v)) {
                    out.labels[key] = nb::cast<std::string>(v);
                } else {
                    out.values[key] = nb::cast<double>(v);
                }
            }
        } catch (const nb::cast_error&) {
            abort_with_type_error(
                "profiler manager get_result() must return a flat dict with string keys and "
                "string or numeric values, or None");
        }
    }

   private:
    // Written by the profiler pass and read by timed-run worker threads.
    std::atomic<bool> active_{false};
};

// GB holds raw pointers to these for the length of the run.
std::unique_ptr<PyMemoryManager> g_memory;
std::unique_ptr<PyProfilerManager> g_profiler;

}  // namespace

void mew_profiler_pause() {
    if (g_profiler) g_profiler->Pause();
}

void mew_profiler_resume() {
    if (g_profiler) g_profiler->Resume();
}

void register_managers(nb::module_& m) {
    m.def(
        "register_memory_manager",
        [](nb::object obj) {
            auto manager = std::make_unique<PyMemoryManager>(std::move(obj));
            // Replaces any registered manager, as upstream does; ours is
            // released only after GB stops pointing at it.
            benchmark::RegisterMemoryManager(manager.get());
            g_memory = std::move(manager);
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's memory manager, replacing any other.\n"
        "Requires `start()` and `stop()`; `stop()` returns memory metrics or None.\n"
        "Pair with `unregister_memory_manager`.");
    m.def("unregister_memory_manager", [] {
        benchmark::RegisterMemoryManager(nullptr);
        g_memory.reset();
    });

    m.def(
        "register_profiler_manager",
        [](nb::object obj) {
            auto manager = std::make_unique<PyProfilerManager>(std::move(obj));
            // GB's BM_CHECK forbids overwriting a registered profiler manager.
            benchmark::RegisterProfilerManager(nullptr);
            benchmark::RegisterProfilerManager(manager.get());
            g_profiler = std::move(manager);
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's profiler manager, replacing any other.\n"
        "Requires `after_setup_start()` and `before_teardown_stop()`; supports optional\n"
        "`get_result()`, `pause()`, and `resume()` hooks.\n"
        "Pair with `unregister_profiler_manager`.");
    m.def("unregister_profiler_manager", [] {
        benchmark::RegisterProfilerManager(nullptr);
        g_profiler.reset();
    });
}
