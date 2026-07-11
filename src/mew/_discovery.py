"""Pytest-style benchmark discovery: walk paths, glob for files, import them.

Internal: consumed by the CLI; not part of the public
API and carries no stability guarantee.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import importlib.util
import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Tracked so discovered() drops exactly what import_file added, and nothing else.
_loaded_modules: list[str] = []
_inserted_paths: list[str] = []


@dataclass(slots=True)
class Selector:
    """One CLI argument decomposed into a filesystem path and an optional filter.

    The filter is a regex (``re.search``) matched against the full benchmark name,
    e.g. ``benchmarks/bench_sort.py::quicksort``.
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
    patterns = list(file_patterns)
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
            # One tree walk matched against every pattern; rglob would re-walk
            # the tree once per pattern, and this also runs on every Tab press
            # (the completion cache's freshness check). A pattern without a `/`
            # matches file names at any depth (rglob-style); one with a `/`
            # matches the path relative to the selector root.
            candidates = []
            for dirpath, _, filenames in os.walk(path):
                reldir = os.path.relpath(dirpath, path)
                for fname in filenames:
                    rel = fname if reldir == os.curdir else os.path.join(reldir, fname)
                    # Slash-separated for matching, so `/` patterns work on Windows.
                    rel = rel.replace(os.sep, "/")
                    if any(fnmatch.fnmatch(rel if "/" in pat else fname, pat) for pat in patterns):
                        candidates.append(Path(dirpath, fname))
            candidates.sort()
        for p in candidates:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def import_file(path: Path) -> None:
    """Import ``path`` as a module; decorator side-effects populate :data:`REGISTRY`.

    Prepends the parent dir to ``sys.path`` (pytest ``prepend`` mode) so a bench file
    can import a sibling; left in place so run-time-deferred imports still resolve.
    """
    # Stable module name from the resolved path so reimports are no-ops; a
    # content-addressed digest keeps it deterministic across processes and
    # collision-resistant, unlike the salted built-in hash.
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode()).hexdigest()[:16]
    mod_name = f"mew._bench_{digest}"
    if mod_name in sys.modules:
        return
    parent = str(resolved.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
        _inserted_paths.append(parent)
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
    _loaded_modules.append(mod_name)


@contextmanager
def discovered() -> Iterator[None]:
    """Unload whatever was imported in the block at exit.

    Wrap collection *and the run* so modules stay live during execution, then get
    cleaned up at the boundary. Only additions made inside the block are undone.
    """
    mod_mark = len(_loaded_modules)
    path_mark = len(_inserted_paths)
    try:
        yield
    finally:
        while len(_loaded_modules) > mod_mark:
            sys.modules.pop(_loaded_modules.pop(), None)
        while len(_inserted_paths) > path_mark:
            with contextlib.suppress(ValueError):
                sys.path.remove(_inserted_paths.pop())
