"""Module-level registry that ``@benchmark`` / ``@parametrize`` populate.

Process-global so files imported from anywhere (discovery, manual import, REPL)
accumulate into one place.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

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
    cases : list[int], optional
        Selected case indices; ``None`` selects every case.
    """

    name: str
    fn: BenchmarkFn
    file: str | None = None
    options: BenchmarkOptions = field(default_factory=lambda: BenchmarkOptions())
    tags: frozenset[str] = field(default_factory=frozenset)
    # Set on parametrized families: `fn` is a trampoline keyed by state.range(0).
    case_labels: list[str] | None = None
    # ``None`` runs every case; a subset only exists on views from narrow_entry().
    cases: list[int] | None = None


def case_names(entry: Entry) -> Iterator[tuple[int, str]]:
    """Yield ``(case_index, name[label])`` for each case of a family.

    A ``-k`` regex matches against these; the form is the row's ``benchmark``
    field, so one pattern selects the same set in ``mew run`` and ``mew compare``.
    """
    for i, label in enumerate(entry.case_labels or ()):
        yield i, f"{entry.name}[{label}]"


def _match_cases(entry: Entry, rx: re.Pattern[str], n: int) -> set[int]:
    """Return the indices of the ``n`` cases a regex selects; a plain benchmark has one."""
    # Plain benchmark, or a family whose own name matches → the whole entry.
    if rx.search(entry.name):
        return set(range(n))
    return {i for i, name in case_names(entry) if rx.search(name)}


def narrow_entry(entry: Entry, filters: Iterable[re.Pattern[str]]) -> Entry | None:
    """Narrow an entry to the cases any of ``filters`` selects, or return ``None``.

    A family whose own name matches keeps all its cases. A view narrows further, so
    narrowing by one filter group, then another, keeps the cases both select. The
    entry is never mutated; a strict subset is a copy.
    """
    n = len(entry.case_labels) if entry.case_labels is not None else 1
    current = set(range(n)) if entry.cases is None else set(entry.cases)
    cases = current & set().union(*(_match_cases(entry, f, n) for f in filters))
    if not cases:
        return None
    return entry if cases == current else dataclasses.replace(entry, cases=sorted(cases))


class Registry:
    """Process-global collection populated by benchmark decorators."""

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
                f"a benchmark named {entry.name!r} is already registered; "
                "pass a unique name= to disambiguate"
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
        """Select entries by name and tags, like ``mew run -k PATTERN --tag TAG``.

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
