"""Child entrypoint for ``mew run --variant``: run one variant's suite, emit JSONL on stdout.

Mutually-incompatible variants (engines that statically link the same library,
GIL vs free-threaded interpreters, …) can't share an interpreter, so each runs
in its own process. This is that process: it imports one benchmark file, runs
the real Google Benchmark timing loop via :func:`mew.run`, and streams a
:class:`~mew.JSONLReporter` document to stdout. The parent
(:mod:`mew._variants`) is the single writer — it re-stamps each row with the
shared ``session_id``, the ``variant`` name, and the repetition index, then
fans out to the user's real sinks. So this worker is deliberately
variant-unaware: it just measures and prints.

Profiling flags (``--profile-memory``, ``--sample`` and friends) mirror
``mew run``: the worker runs the out-of-loop profile pass and wraps its reporter
in :class:`~mew._profile._ProfileEnriching`, so each row carries its ``memory`` /
``cpu_profile`` block. The parent re-emits those per variant, making cross-engine
memory/CPU comparison work from one ``--variant`` run. Profiling runs once per
child invocation (i.e. per repetition); any HTML artifact path is already
suffixed with the variant name by the parent.

Invoked as::

    python -m mew._variant_worker --file F [--pattern P] [--tag T ...] [--gb ARG ...]
        [--profile-memory] [--flamegraph PATH] [--sample] [--sample-interval F]
        [--sample-iterations N] [--sample-html PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mew import discovery as _discovery
from mew._registry import REGISTRY
from mew.reporter import JSONLReporter, Reporter
from mew.runner import run as _run


def _build_reporter(ns: argparse.Namespace, entries: list) -> Reporter:
    """JSONL-to-stdout reporter, wrapped to attach profiles when flags are set."""
    reporter: Reporter = JSONLReporter(output=None)
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
    if memory_profiles is not None or cpu_profiles is not None:
        from mew._profile import _ProfileEnriching

        reporter = _ProfileEnriching(
            reporter, memory_profiles=memory_profiles, cpu_profiles=cpu_profiles
        )
    return reporter


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mew._variant_worker")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--pattern", default=None)
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--gb", action="append", default=[], help="raw Google Benchmark arg")
    ap.add_argument("--profile-memory", action="store_true", dest="profile_memory")
    ap.add_argument("--flamegraph", default=None, type=Path)
    ap.add_argument("--memory-iterations", default=100, type=int, dest="memory_iterations")
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--sample-interval", default=1e-4, type=float, dest="sample_interval")
    ap.add_argument("--sample-iterations", default=1000, type=int, dest="sample_iterations")
    ap.add_argument("--sample-html", default=None, type=Path, dest="sample_html")
    ns = ap.parse_args(argv)

    REGISTRY.clear()
    _discovery.import_file(ns.file)
    try:
        entries = REGISTRY.filter(ns.pattern, tags=ns.tag or None)
    except ValueError as e:
        print(f"mew: {e}", file=sys.stderr)
        return 1
    if not entries:
        print(f"mew: no benchmarks in {ns.file}", file=sys.stderr)
        return 1

    # JSONL to stdout (output=None). The parent parses these lines; its own
    # session id and the variant name are applied there, not here.
    _run(entries, argv=["mew", *ns.gb], reporter=_build_reporter(ns, entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
