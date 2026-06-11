"""Child entrypoint launched by an out-of-process profiler to run one benchmark case.

Profiler-agnostic: xctrace, py-spy, and perf all sample the *whole process* from
the outside (which is how they capture native C-extension frames that in-process
samplers like pyinstrument miss), so the benchmark body has to live in a separate
process they launch. This is that process — each backend wraps the same worker
argv with its own recording prefix (see :mod:`mew.profilers`).

Invoked as::

    python -m mew._subprocess_worker --file F --entry NAME --case I --iterations N

It re-imports the benchmark file (decorator side-effects repopulate
:data:`REGISTRY`), looks the entry up by name, and drives its body in a tight
loop via :class:`mew._profile._ProfileState` — the same out-of-loop runner the
memory/CPU profilers use, so ``state.range``/family trampolines behave
identically. Runs in a fresh interpreter, so the ``sys.modules`` caching in
:func:`mew.discovery.import_file` never bites here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mew import discovery as _discovery
from mew._profile import _ProfileState
from mew._registry import REGISTRY


def run_case(file: Path, entry_name: str, case: int, iterations: int) -> int:
    """Import ``file``, find ``entry_name``, and run its body ``iterations`` times."""
    REGISTRY.clear()
    _discovery.import_file(file)
    entry = next((e for e in REGISTRY.all() if e.name == entry_name), None)
    if entry is None:
        print(f"mew: benchmark not found: {entry_name}", file=sys.stderr)
        return 1
    # range_value drives the family trampoline (state.range(0)); no pause factory
    # because there's no in-process sampler to suspend out here.
    entry.fn(_ProfileState(n_iterations=iterations, range_value=case))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mew._subprocess_worker")
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--entry", required=True)
    ap.add_argument("--case", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=100_000)
    ns = ap.parse_args(argv)
    return run_case(ns.file, ns.entry, ns.case, ns.iterations)


if __name__ == "__main__":
    raise SystemExit(main())
