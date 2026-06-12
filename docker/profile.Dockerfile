# Image for exercising the py-spy backend of `mew profile` on Linux.
#
# py-spy's `--native` needs CAP_SYS_PTRACE even for processes it launches itself
# (it reads target memory via process_vm_readv/ptrace), so it must be added at run
# time. The default cap set in both runtimes below excludes it.
#
# On Apple silicon (macOS 26+), prefer Apple's `container` — it runs an arm64 VM
# *natively*, which py-spy --native needs (an emulated amd64 guest breaks ptrace):
#
#   container build -t mew-profile -f docker/profile.Dockerfile .
#   container run --rm --cap-add SYS_PTRACE mew-profile
#
# Docker works the same way (let it resolve to the host arch — don't force
# --platform=linux/amd64, that's emulated):
#
#   docker build -f docker/profile.Dockerfile -t mew-profile .
#   docker run --rm --cap-add=SYS_PTRACE mew-profile
#
# If ptrace is still blocked despite the cap, a default seccomp profile is the
# likely cause; Apple's container doesn't yet let you override seccomp
# (apple/containerization#551), so fall back to a full Linux VM (Lima/Colima/UTM).
#
# perf is deliberately NOT tested here — its binary is tied to the guest kernel
# and needs CAP_PERFMON/SYS_ADMIN or a lowered perf_event_paranoid; test perf on a
# real Linux box / VM instead (see docs/development/contributing.md).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . .

# Build mew's C core (test group), then add the py-spy backend (no longer a
# project extra — installed straight into the synced venv).
RUN uv sync --group test
RUN uv pip install py-spy

CMD ["uv", "run", "pytest", "tests/test_profilers_native.py", "-v", "-k", "pyspy"]
