// Python adapters for Google Benchmark's process-global managers.

#include "managers.h"

#include <benchmark/benchmark.h>
#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>

#include <atomic>
#include <exception>
#include <memory>
#include <string>

#include "abort.h"

namespace nb = nanobind;
using namespace nb::literals;

namespace {

void set_manager_result_type_error(const char* message) {
    try {
        throw nb::type_error(message);
    } catch (...) {
        mew_set_pending_abort(std::current_exception());
    }
}

// Calls the Python manager without unwinding through Google Benchmark.
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
    // The caller holds the GIL for the returned object's lifetime.
    nb::object call(const char* name) {
        try {
            return py_.attr(name)();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return nb::none();
        }
    }

    nb::object py_;
};

// Keys the manager omits keep their tombstone and are dropped by `Run.to_dict`.
bool fill_memory_result(const nb::dict& d, benchmark::MemoryManager::Result& out) {
    auto set = [&](const char* key, int64_t& target) {
        if (!d.contains(key)) return true;
        int64_t value;
        if (!nb::try_cast(d[key], value)) return false;
        target = value;
        return true;
    };
    return set("total_allocations", out.num_allocs) && set("peak_bytes", out.max_bytes_used) &&
           set("total_bytes", out.total_allocated_bytes) &&
           set("net_heap_growth", out.net_heap_growth);
}

// Split a flat Python mapping into Google Benchmark's label and value maps.
bool fill_profile_result(const nb::dict& d, benchmark::ProfilerManager::Result& out) {
    for (auto [k, v] : d) {
        std::string key;
        if (!nb::try_cast(k, key, false)) return false;
        if (nb::isinstance<nb::str>(v)) {
            std::string value;
            if (!nb::try_cast(v, value, false)) return false;
            out.labels[key] = std::move(value);
        } else {
            double value;
            if (!nb::try_cast(v, value)) return false;
            out.values[key] = value;
        }
    }
    return true;
}

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
        if (!nb::isinstance<nb::dict>(r) || !fill_memory_result(nb::borrow<nb::dict>(r), out)) {
            set_manager_result_type_error(
                "memory manager stop() must return a dict with integer values, or None");
        }
    }
};

class PyProfilerManager final : public benchmark::ProfilerManager, public PyManager {
   public:
    explicit PyProfilerManager(nb::object obj) : PyManager(std::move(obj)) {
        // Cache optional hooks used inside benchmark loops.
        nb::gil_scoped_acquire gil;
        if (nb::hasattr(py_, "pause")) pause_ = py_.attr("pause");
        if (nb::hasattr(py_, "resume")) resume_ = py_.attr("resume");
        if (nb::hasattr(py_, "get_result")) get_result_ = py_.attr("get_result");
    }

    ~PyProfilerManager() override {
        if (!Py_IsInitialized()) {
            pause_.release();
            resume_.release();
            get_result_.release();
            return;
        }
        nb::gil_scoped_acquire gil;
        pause_.reset();
        resume_.reset();
        get_result_.reset();
    }

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

    // Only forward pauses during the profiler pass.
    void Pause() {
        if (active_) invoke(pause_);
    }
    void Resume() {
        if (active_) invoke(resume_);
    }

    void GetResult(Result& out) override {
        if (!get_result_.is_valid()) return;
        nb::gil_scoped_acquire gil;
        nb::object r;
        try {
            r = get_result_();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
            return;
        }
        if (r.is_none()) return;
        if (!nb::isinstance<nb::dict>(r) || !fill_profile_result(nb::borrow<nb::dict>(r), out)) {
            set_manager_result_type_error(
                "profiler manager get_result() must return a flat dict with string keys and "
                "string or numeric values, or None");
        }
    }

   private:
    void invoke(const nb::object& fn) {
        if (!fn.is_valid()) return;
        nb::gil_scoped_acquire gil;
        try {
            fn();
        } catch (...) {
            mew_set_pending_abort(std::current_exception());
        }
    }

    // Written by the profiler pass and read by timed-run worker threads.
    std::atomic<bool> active_{false};
    nb::object pause_;
    nb::object resume_;
    nb::object get_result_;
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
            if (g_memory) throw nb::value_error("a memory manager is already registered");
            auto manager = std::make_unique<PyMemoryManager>(std::move(obj));
            benchmark::RegisterMemoryManager(manager.get());
            g_memory = std::move(manager);
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's memory manager.\n"
        "Requires `start()` and `stop()`; `stop()` returns memory metrics or None.\n"
        "Pair with `unregister_memory_manager`.");
    m.def("unregister_memory_manager", [] {
        benchmark::RegisterMemoryManager(nullptr);
        g_memory.reset();
    });

    m.def(
        "register_profiler_manager",
        [](nb::object obj) {
            if (g_profiler) throw nb::value_error("a profiler manager is already registered");
            auto manager = std::make_unique<PyProfilerManager>(std::move(obj));
            benchmark::RegisterProfilerManager(manager.get());
            g_profiler = std::move(manager);
        },
        "manager"_a,
        "Register `manager` as Google Benchmark's profiler manager.\n"
        "Requires `after_setup_start()` and `before_teardown_stop()`; supports optional\n"
        "`get_result()`, `pause()`, and `resume()` hooks.\n"
        "Pair with `unregister_profiler_manager`.");
    m.def("unregister_profiler_manager", [] {
        benchmark::RegisterProfilerManager(nullptr);
        g_profiler.reset();
    });
}
