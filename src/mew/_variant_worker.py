"""Child entrypoint for ``mew run --variant``: run one variant's suite, emit JSONL on stdout.

Mutually-incompatible variants can't share an interpreter, so each runs in its
own process. This is that process: it imports one benchmark file, runs the
timing loop via :func:`mew.run`, and streams a :class:`~mew.JSONLReporter`
document to stdout. The parent (:mod:`mew._variants`) is the single writer, so
this worker is variant-unaware; it just measures and prints.

Profiling flags mirror ``mew run``: the worker runs the out-of-loop profile pass
and hands the results to :func:`mew.run`, whose projector attaches each row's
``memory`` / ``cpu_profile`` block. Profiling runs once per child invocation; any
HTML artifact path is already suffixed with the variant name by the parent.

Invoked as::

    python -m mew._variant_worker --file F [--pattern P] [--tag T ...]
        [--min-time V] [--min-warmup-time S] [--random-interleaving]
        [--profile-memory] [--flamegraph PATH] [--sample] [--sample-interval F]
        [--sample-iterations N] [--sample-html PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mew import discovery as _discovery
from mew._registry import REGISTRY
from mew.reporter import JSONLReporter
from mew.runner import run as _run


def _profiles(ns: argparse.Namespace, entries: list) -> tuple[dict | None, dict | None]:
    """Run the out-of-loop profile passes the flags ask for, as run() expects them."""
    memory_profiles = None
    cpu_profiles = None
    if ns.profile_memory or ns.flamegraph is not None:
        from mew.memory import profile as _profile_mem

        memory_profiles = _profile_mem(
            entries, flamegraph=ns.flamegraph, iterations=ns.memory_iterations
        )
    if ns.sample or ns.sample_html is not None:
        from mew.cpu import profile as _profile_cpu

        cpu_profiles = _profile_cpu(
            entries,
            output=ns.sample_html,
            interval=ns.sample_interval,
            inner_iterations=ns.sample_iterations,
        )
    return memory_profiles, cpu_profiles


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mew._variant_worker")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--pattern", default=None)
    ap.add_argument("--literal", action="store_true")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--min-time", default=None, dest="min_time")
    ap.add_argument("--min-warmup-time", default=None, type=float, dest="min_warmup_time")
    ap.add_argument("--random-interleaving", action="store_true", dest="random_interleaving")
    ap.add_argument("--profile-memory", action="store_true", dest="profile_memory")
    ap.add_argument("--flamegraph", default=None, type=Path)
    ap.add_argument("--memory-iterations", default=100, type=int, dest="memory_iterations")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--sample-interval", default=1e-4, type=float, dest="sample_interval")
    ap.add_argument("--sample-iterations", default=1000, type=int, dest="sample_iterations")
    ap.add_argument("--sample-html", default=None, type=Path, dest="sample_html")
    ns = ap.parse_args(argv)

    # Isolate the data channel: the parent parses our stdout as JSONL, and a
    # stray print from user code or GB would corrupt it. Keep the real stdout
    # fd for the reporter and point fd 1 at stderr, the never-parsed channel.
    sys.stdout.flush()
    data_fd = os.dup(1)
    os.dup2(2, 1)
    data_stream = os.fdopen(data_fd, "w")
    try:
        REGISTRY.clear()
        _discovery.import_file(ns.file)
        try:
            entries = REGISTRY.filter(ns.pattern, tags=ns.tag or None, literal=ns.literal)
        except ValueError as e:
            print(f"mew: {e}", file=sys.stderr)
            return 1
        if not entries:
            print(f"mew: no benchmarks in {ns.file}", file=sys.stderr)
            return 1

        # JSONL to the preserved stdout; the parent applies its session id and the
        # variant name when it parses these lines. `header=True`: the channel
        # carries the full context line, which the parent needs to re-project
        # (executable / MHz / build type are not stamped per row).
        memory_profiles, cpu_profiles = _profiles(ns, entries)
        _run(
            entries,
            min_time=ns.min_time,
            min_warmup_time=ns.min_warmup_time,
            random_interleaving=ns.random_interleaving,
            reporter=JSONLReporter(output=data_stream, header=True),
            memory_profiles=memory_profiles,
            cpu_profiles=cpu_profiles,
        )
        return 0
    finally:
        data_stream.flush()
        data_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
