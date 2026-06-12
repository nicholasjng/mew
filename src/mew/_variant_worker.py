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

Invoked as::

    python -m mew._variant_worker --file F [--pattern P] [--tag T ...] [--gb ARG ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mew import discovery as _discovery
from mew._registry import REGISTRY
from mew.reporter import JSONLReporter
from mew.runner import run as _run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mew._variant_worker")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--pattern", default=None)
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--gb", action="append", default=[], help="raw Google Benchmark arg")
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
    _run(entries, argv=["mew", *ns.gb], reporter=JSONLReporter(output=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
