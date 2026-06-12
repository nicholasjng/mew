# Image for exercising the py-spy backend of `mew profile` on Linux.
#
# py-spy's `--native` needs CAP_SYS_PTRACE even for processes it launches itself
# (it reads target memory via process_vm_readv/ptrace), so run with:
#
#   docker build -f docker/profile.Dockerfile -t mew-profile .
#   docker run --rm --cap-add=SYS_PTRACE mew-profile
#
# No --privileged required. perf is deliberately NOT tested here — its binary is
# tied to the host kernel and needs CAP_PERFMON/SYS_ADMIN or a lowered
# perf_event_paranoid; test perf on a real Linux box instead (see
# docs/development/contributing.md).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY . .

# Build mew's C core and install the py-spy backend extra.
RUN uv sync --extra profile --group test

CMD ["uv", "run", "pytest", "tests/test_profilers_native.py", "-v", "-k", "pyspy"]
