"""Discover and import benchmark modules."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import importlib.util
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mew._registry import Entry, compile_name_filter, narrow_entry

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


@dataclass(frozen=True, slots=True)
class ResolvedSelector:
    """A selector with filesystem state and its name filter resolved once."""

    path: Path
    is_directory: bool
    pattern: re.Pattern[str] | None

    @classmethod
    def resolve(cls, selector: Selector, *, literal: bool = False) -> ResolvedSelector:
        path = selector.path.resolve()
        pattern = compile_name_filter(selector.filter, literal=literal) if selector.filter else None
        return cls(path, path.is_dir(), pattern)

    def includes(self, source: Path) -> bool:
        return source == self.path or (self.is_directory and source.is_relative_to(self.path))


def select_entries(
    candidates: Iterable[tuple[Entry, Path | None]],
    selectors: Sequence[ResolvedSelector],
    *,
    names: Sequence[re.Pattern[str]] = (),
    pattern: re.Pattern[str] | None = None,
    tags: Iterable[str] = (),
) -> list[Entry]:
    """Select entries without filesystem access, imports, or registry mutation.

    Each candidate pairs an entry with its resolved source path. Path selectors
    and stdin names form an OR group; the global pattern and tags narrow it.
    An unfiltered path selects everything beneath it unless stdin names restrict
    that discovery path. Family filters produce views without changing entries.
    """
    wanted = set(tags)
    selected = []
    for entry, source in candidates:
        if wanted and not wanted.intersection(entry.tags):
            continue
        applicable = [
            selector.pattern
            for selector in selectors
            if source is not None and selector.includes(source)
        ]
        if not applicable:
            continue
        unrestricted = None in applicable and not names
        filters = [] if unrestricted else [rx for rx in applicable if rx is not None] + list(names)
        narrowed = narrow_entry(entry, any_of=filters, all_of=pattern)
        if narrowed is not None:
            selected.append(narrowed)
    return selected


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
    """Import a benchmark file, allowing imports from its parent directory."""
    # Use a stable, collision-resistant module name.
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
