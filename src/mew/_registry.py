"""The benchmark registry, its entries and name filters, and benchmark discovery.

:data:`REGISTRY` is process-global, so benchmark files imported from anywhere
register into one place. Discovery finds benchmark files under the paths a
command names, imports them into the registry, and undoes that afterwards
(:func:`discovered`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import hashlib
import importlib.util
import os
import re
import sys
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from mew._options import thread_counts
from mew._typing import BenchmarkFn, BenchmarkOptions


def compile_name_filter(pattern: str, *, literal: bool = False) -> re.Pattern[str]:
    """Compile a benchmark-name filter into an unanchored regex.

    Matched with ``re.search``, like GB's ``--benchmark_filter``. ``literal=True``
    escapes the pattern so a displayed ``name[label]`` matches as-is. Raises
    :class:`ValueError` on a malformed pattern.
    """
    try:
        return re.compile(re.escape(pattern) if literal else pattern)
    except re.error as e:
        raise ValueError(f"invalid benchmark filter pattern {pattern!r}: {e}") from e


@dataclass(slots=True)
class Entry:
    """One registered benchmark or parametrized family.

    Attributes
    ----------
    name : str
        Registered benchmark name.
    fn : BenchmarkFn
        Callable passed to the native runner.
    file : str, optional
        Source file.
    options : BenchmarkOptions
        Per-benchmark Google Benchmark options.
    tags : frozenset[str]
        Selection tags.
    case_labels : list[str], optional
        Human-readable family case labels.
    case_params : list[dict], optional
        Each family case's parameters as result rows hold them: JSON scalars,
        other values as their type's qualified name.
    cases : list[int], optional
        Selected case indices; ``None`` selects every case.
    """

    name: str
    fn: BenchmarkFn
    file: str | None = None
    options: BenchmarkOptions = field(default_factory=lambda: BenchmarkOptions())
    tags: frozenset[str] = field(default_factory=frozenset)
    # Set on parametrized families: `fn` is a trampoline that takes the case index.
    case_labels: list[str] | None = None
    case_params: list[dict[str, Any]] | None = None
    # ``None`` runs every case; a subset only exists on views from narrow_entry().
    cases: list[int] | None = None


def case_names(entry: Entry) -> Iterator[tuple[int, str]]:
    """Yield ``(case_index, name[label])`` for each case of a family.

    A ``-k`` regex matches against these; the form is the row's ``benchmark``
    field, so one pattern selects the same set in ``mew run`` and ``mew compare``.
    """
    for i, label in enumerate(entry.case_labels or ()):
        yield i, f"{entry.name}[{label}]"


def selected_cases(entry: Entry) -> Iterator[tuple[int, str]]:
    """Yield ``(case_index, name[label])`` for each case a name filter left selected."""
    for i, name in case_names(entry):
        if entry.cases is None or i in entry.cases:
            yield i, name


def longest_row_name(entries: Iterable[Entry]) -> int:
    """Return the length of the longest row name these entries report.

    It counts the selected cases' labels and a ``/threads:N`` suffix.
    """
    longest = 0
    for entry in entries:
        cases = [name for _, name in selected_cases(entry)]
        width = max(map(len, cases or [entry.name]))
        if entry.options.get("threads") is not None:
            width += len(f"/threads:{max(thread_counts(entry.options))}")
        longest = max(longest, width)
    return longest


def narrow_entry(entry: Entry, filters: Iterable[re.Pattern[str]]) -> Entry | None:
    """Narrow an entry to the cases any of ``filters`` selects, or return ``None``.

    A family whose own name matches keeps all its cases. A view narrows further, so
    narrowing by one filter group, then another, keeps the cases both select. The
    entry is never mutated; a strict subset is a copy.
    """
    n = len(entry.case_labels) if entry.case_labels is not None else 1
    current = set(range(n)) if entry.cases is None else set(entry.cases)
    selected: set[int] = set()
    for rx in filters:
        # A plain benchmark, or a family whose own name matches: the whole entry.
        if rx.search(entry.name):
            selected.update(range(n))
        else:
            selected.update(i for i, name in case_names(entry) if rx.search(name))
    cases = current & selected
    if not cases:
        return None
    return entry if cases == current else dataclasses.replace(entry, cases=sorted(cases))


class Registry:
    """A collection of benchmark entries with unique names, in registration order."""

    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def add(self, entry: Entry) -> None:
        """Add an entry.

        Parameters
        ----------
        entry : Entry
            Entry to register.

        Raises
        ------
        ValueError
            If its name is already registered.
        """
        # Two entries sharing a name would run as indistinguishable rows and
        # merge into one distribution on compare.
        if entry.name in self._entries:
            raise ValueError(
                f"{entry.name}: already registered\nhint: pass name= to tell them apart"
            )
        self._entries[entry.name] = entry

    def clear(self) -> None:
        """Remove all entries."""
        self._entries.clear()

    def all(self) -> list[Entry]:
        """Return a copy of all entries.

        Returns
        -------
        list[Entry]
            Entries in registration order.
        """
        return list(self._entries.values())

    def filter(
        self,
        pattern: str | None = None,
        *,
        tags: Iterable[str] | None = None,
        literal: bool = False,
    ) -> list[Entry]:
        """Select entries by a name pattern and tags.

        Parameters
        ----------
        pattern : str, optional
            Name regex, searched for (``re.search``). A match on some cases of a
            family narrows the family to those cases.
        tags : Iterable[str], optional
            Selects entries with any of these tags.
        literal : bool, default False
            Match ``pattern`` as plain text, e.g. to keep a ``name[label]``'s
            brackets, like ``mew run -F``.

        Returns
        -------
        list[Entry]
            Matching entries in registration order.

        Raises
        ------
        ValueError
            If ``pattern`` is not a valid regex.
        """
        out = list(self._entries.values())
        if tags:
            wanted = set(tags)
            out = [e for e in out if wanted.intersection(e.tags)]
        if pattern:
            rx = compile_name_filter(pattern, literal=literal)
            out = [n for e in out if (n := narrow_entry(e, [rx])) is not None]
        return out

    def __len__(self) -> int:
        return len(self._entries)


REGISTRY = Registry()


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
    """Undo what discovering and running a suite changed in the process, on exit.

    That is the benchmark modules and paths added within the context, and the
    registry, so one ``main()`` call in a process, such as a notebook or a
    test, doesn't leak into the next or clear what was registered before it.
    """
    entries = REGISTRY.all()
    mod_mark = len(_loaded_modules)
    path_mark = len(_inserted_paths)
    try:
        yield
    finally:
        REGISTRY.clear()
        for entry in entries:
            REGISTRY.add(entry)
        while len(_loaded_modules) > mod_mark:
            sys.modules.pop(_loaded_modules.pop(), None)
        while len(_inserted_paths) > path_mark:
            with contextlib.suppress(ValueError):
                sys.path.remove(_inserted_paths.pop())
