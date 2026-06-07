"""Pytest-style benchmark discovery: walk paths, glob for files, import them."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Selector:
    """One CLI argument decomposed into a filesystem path and an optional filter.

    The filter is matched as a substring against the full benchmark name (e.g. ``benchmarks/bench_sort.py::quicksort``).
    """

    path: Path
    filter: str | None = None


def parse(arg: str) -> Selector:
    if "::" in arg:
        path, _, flt = arg.partition("::")
        return Selector(Path(path), flt or None)
    return Selector(Path(arg))


def collect_files(
    selectors: Sequence[Selector],
    *,
    file_patterns: Iterable[str],
) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for sel in selectors:
        path = sel.path.resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        candidates: list[Path]
        if path.is_file():
            candidates = [path]
        else:
            candidates = []
            for pat in file_patterns:
                candidates.extend(sorted(path.rglob(pat)))
        for p in candidates:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def import_file(path: Path) -> None:
    """Import ``path`` as a module.

    Decorator side-effects populate :data:`REGISTRY`.
    """
    # Stable module name derived from the resolved path so reimports are cheap.
    mod_name = f"mew._bench_{abs(hash(path.resolve()))}"
    if mod_name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load benchmark module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
