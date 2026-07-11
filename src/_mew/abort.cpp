#include "abort.h"

#include <mutex>
#include <utility>

namespace {
std::mutex g_mutex;
std::exception_ptr g_pending;
}  // namespace

void mew_set_pending_abort(std::exception_ptr p) {
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_pending) g_pending = std::move(p);
}

bool mew_abort_pending() {
    std::lock_guard<std::mutex> lock(g_mutex);
    return static_cast<bool>(g_pending);
}

std::exception_ptr mew_take_pending_abort() {
    std::lock_guard<std::mutex> lock(g_mutex);
    return std::exchange(g_pending, nullptr);
}
