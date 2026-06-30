"""Pytest-style benchmark discovery: walk paths, glob for files, import them."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Tracked so unload() drops exactly what import_file added, and nothing else.
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
    """Import ``path`` as a module; decorator side-effects populate :data:`REGISTRY`.

    Prepends the parent dir to ``sys.path`` (pytest ``prepend`` mode) so a bench file
    can import a sibling; left in place so run-time-deferred imports still resolve.
    """
    # Stable module name from the resolved path so reimports are no-ops. A
    # content-addressed digest (not the salted built-in hash) keeps the name
    # deterministic across processes and collision-resistant, so two distinct
    # bench files can't map to the same synthetic module and shadow each other.
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


def unload() -> None:
    """Drop the synthetic bench modules and ``sys.path`` entries import_file added.

    Safe post-*run*: each ``Entry.fn`` keeps its namespace alive via ``__globals__``,
    so the pop doesn't break execution. Sibling/third-party modules are left alone;
    pruning them from ``sys.modules`` risks half-initialized-module bugs.
    """
    while _loaded_modules:
        sys.modules.pop(_loaded_modules.pop(), None)
    while _inserted_paths:
        with contextlib.suppress(ValueError):
            sys.path.remove(_inserted_paths.pop())


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
