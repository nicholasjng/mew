"""Live tests for the py-spy and perf backends.

Skipped unless the tool is actually *usable* here, so the suite stays green on
macOS and dev machines and only exercises a backend where it can run (Linux CI, a
VM, or the docker/profile.Dockerfile image). Assertions check *artifact validity*
— a valid speedscope schema, a non-empty perf script — not specific frame
contents, which depend on symbols/kernel and would be flaky.

perf needs more than the binary: `perf_event_paranoid` gates access for processes
without CAP_PERFMON/SYS_ADMIN, and GitHub-hosted runners ship it locked (=4) and
don't let you lower it. The backend's `unavailable_reason()` already probes
whether perf can record and returns an actionable message if not — we reuse it
here so the test skips with the *same* reason a user would see, rather than
failing. To actually run perf, grant the capability (privileged/capped container)
or lower perf_event_paranoid — see docs/development/contributing.md.
"""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from mew import discovery
from mew._registry import REGISTRY
from mew.profilers.perf import PerfProfiler

requires_pyspy = pytest.mark.skipif(
    sys.platform == "darwin" or shutil.which("py-spy") is None,
    reason="needs py-spy on Linux/Windows (+ CAP_SYS_PTRACE)",
)
# Single source of truth: the backend decides (and explains) usability.
_perf_reason = PerfProfiler().unavailable_reason()
requires_perf = pytest.mark.skipif(_perf_reason is not None, reason=_perf_reason or "perf usable")

_SRC = textwrap.dedent(
    """
    import mew

    @mew.benchmark
    def bench_a(state):
        for _ in state:
            sum(range(50))
    """
)


def _discover_one(tmp_path: Path):
    bench = tmp_path / "bench_x.py"
    bench.write_text(_SRC)
    REGISTRY.clear()
    discovery.import_file(bench)
    return REGISTRY.all()[0]


@requires_pyspy
def test_pyspy_emits_valid_speedscope(tmp_path):
    from mew.profilers.pyspy import PySpyProfiler

    entry = _discover_one(tmp_path)
    artifacts = PySpyProfiler().run([entry], output_dir=tmp_path, iterations=300_000)

    assert artifacts
    out = next(iter(artifacts.values()))
    assert out.suffix == ".json"
    doc = json.loads(out.read_text())
    assert "speedscope" in doc["$schema"]
    assert doc["profiles"]  # non-empty → samples were collected


@requires_perf
def test_perf_emits_nonempty_script(tmp_path):
    entry = _discover_one(tmp_path)
    artifacts = PerfProfiler().run([entry], output_dir=tmp_path, iterations=300_000)

    assert artifacts
    out = next(iter(artifacts.values()))
    assert out.with_suffix(".data").exists()  # raw perf.data kept alongside
    assert out.read_text().strip()  # perf script produced frames
