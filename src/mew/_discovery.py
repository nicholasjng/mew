"""Discover and import benchmark modules."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import importlib.util
import os
import re
import sys
from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from mew._registry import Entry, compile_name_filter, narrow_entry

# Tracked so discovered() drops exactly what import_file added, and nothing else.
_loaded_modules: list[str] = []
_inserted_paths: list[str] = []

#: Directories discovery doesn't descend into, besides hidden ones and virtualenvs:
#: build output that may hold copies of benchmark files, and other tools' trees.
#: A directory named on the command line is still searched.
_SKIP_DIRS = frozenset({"__pycache__", "build", "dist", "node_modules"})


@dataclass(frozen=True, slots=True)
class Selector:
    """One CLI argument decomposed into a resolved path and an optional filter.

    The filter is a regex (``re.search``) matched against the full benchmark name,
    e.g. ``benchmarks/bench_sort.py::quicksort``. :func:`parse` resolves the path
    and compiles the filter once.
    """

    path: Path
    is_directory: bool
    pattern: re.Pattern[str] | None

    def includes(self, source: Path) -> bool:
        return source == self.path or (self.is_directory and source.is_relative_to(self.path))


def select_entries(
    candidates: Iterable[tuple[Entry, Path | None]],
    selectors: Sequence[Selector],
) -> list[Entry]:
    """Scope entries to path selectors without filesystem access, imports, or registry mutation.

    Each candidate pairs an entry, already selected by ``-k`` and ``--tag`` through
    :meth:`Registry.filter`, with its resolved source path. The filters of the path
    selectors that include it form an OR group, and an unfiltered path selects
    everything beneath it. Family filters produce views without changing entries.
    """
    selected = []
    for entry, source in candidates:
        applicable = [
            selector.pattern
            for selector in selectors
            if source is not None and selector.includes(source)
        ]
        if not applicable:
            continue
        if None in applicable:
            selected.append(entry)
            continue
        filters = [f for f in applicable if f is not None]
        if (narrowed := narrow_entry(entry, filters)) is not None:
            selected.append(narrowed)
    return selected


def parse(arg: str, *, root: Path | None = None, literal: bool = False) -> Selector:
    """Parse ``path[::filter]``; a relative path resolves against ``root``, else the cwd.

    Raises :class:`ValueError` on a malformed filter.
    """
    raw, _, flt = arg.partition("::")
    path = (root / raw if root is not None else Path(raw)).resolve()  # absolute replaces root
    return Selector(path, path.is_dir(), compile_name_filter(flt, literal=literal) if flt else None)


def collect_files(
    selectors: Sequence[Selector],
    *,
    file_patterns: Iterable[str],
) -> list[Path]:
    """Return the files the selectors name, and the matching files beneath their directories.

    A file matches when its name matches one of ``file_patterns``, as pytest's
    ``python_files`` do. The search skips hidden directories, virtualenvs and
    those in ``_SKIP_DIRS`` below a selector's directory, not the directory itself.
    A path that doesn't exist raises :class:`FileNotFoundError`.
    """
    patterns = list(file_patterns)
    out: dict[Path, None] = {}  # an ordered set: the first selector to reach a file wins
    for sel in selectors:
        path = sel.path
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            out[path] = None
            continue
        found = (
            p
            for p in _walk(path)
            if any(fnmatch.fnmatch(p.name, pat) for pat in patterns) and p.is_file()
        )
        out.update(dict.fromkeys(sorted(found)))
    return list(out)


def _walk(root: Path) -> Generator[Path]:
    """The files beneath ``root``, pruning the directories discovery skips."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Pruned in place, so os.walk never lists what's inside them.
        dirnames[:] = [d for d in dirnames if not _skipped(Path(dirpath, d))]
        yield from (Path(dirpath, f) for f in filenames)


def _skipped(directory: Path) -> bool:
    """Whether discovery skips ``directory``: hidden, a virtualenv, or in ``_SKIP_DIRS``."""
    name = directory.name
    if name.startswith(".") or name in _SKIP_DIRS:
        return True
    # A venv or conda environment under any name, such as `env` or `venv-3.14`.
    return (directory / "pyvenv.cfg").is_file() or (directory / "conda-meta").is_dir()


def import_file(path: Path) -> ModuleType:
    """Import a benchmark file, allowing imports from its parent directory.

    Returns the module, also when an earlier call already imported it.
    """
    # Name by path hash so same-named files in different directories don't collide.
    resolved = path.resolve()
    digest = hashlib.sha1(str(resolved).encode()).hexdigest()[:16]
    mod_name = f"mew._bench_{digest}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
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
    return module


@contextmanager
def discovered() -> Generator[None]:
    """Remove benchmark modules and paths added within the context on exit."""
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
