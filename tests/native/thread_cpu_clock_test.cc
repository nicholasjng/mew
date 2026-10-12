// Google Benchmark reads thread CPU time on macOS through thread_info, at whole
// microseconds, unless the deployment target has clock_gettime (10.12+). A
// step between two reads below a microsecond shows clock_gettime is in use.
#include <iostream>

namespace benchmark {
// From src/timers.h, outside the public API.
double ThreadCPUUsage();
}  // namespace benchmark

int main() {
    double previous = benchmark::ThreadCPUUsage();
    for (int i = 0; i < 100000; ++i) {
        const double now = benchmark::ThreadCPUUsage();
        if (now > previous && now - previous < 0.5e-6) return 0;
        previous = now;
    }
    std::cerr << "thread CPU time advances in whole microseconds\n";
    return 1;
}
