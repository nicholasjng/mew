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

    Matched with ``re.search`` (like Google Benchmark's own ``--benchmark_filter``),
    so a plain literal like ``bench_sort`` works as a substring while
    ``bench_(sort|search)`` also works. With ``literal=True`` the pattern is
    :func:`re.escape`-d first, so a displayed ``name[label]`` matches without
    escaping its brackets. Raises :class:`ValueError` on a malformed pattern.
    """
    try:
        return re.compile(re.escape(pattern) if literal else pattern)
    except re.error as e:
        raise ValueError(f"invalid benchmark filter pattern {pattern!r}: {e}") from e


@dataclass(slots=True)
class Entry:
    name: str
    fn: BenchmarkFn
    file: str | None = None
    options: BenchmarkOptions = field(default_factory=lambda: BenchmarkOptions())
    tags: frozenset[str] = field(default_factory=frozenset)
    # Set on parametrized families: `fn` is a trampoline keyed by state.range(0).
    case_labels: list[str] | None = None
    # Case indices a name filter narrowed the family to. ``None`` runs every case
    # (the only value the registry stores); a subset is a transient view from
    # :func:`narrow_entry`, never registered.
    cases: list[int] | None = None


def case_names(entry: Entry) -> Iterator[tuple[int, str]]:
    """Yield ``(case_index, addressable_name)`` for each case of a family.

    A ``-k`` regex matches against these. ``name/case:i`` addresses by index;
    ``name[label]`` addresses by the human label and mirrors
    ``compare._canonical_name``, so one pattern selects the same set in
    ``mew run`` and ``mew compare``.
    """
    for i, label in enumerate(entry.case_labels or ()):
        yield i, f"{entry.name}/case:{i}"
        yield i, f"{entry.name}[{label}]"


@dataclass(frozen=True, slots=True)
class _CaseMatch:
    """Outcome of matching one entry: excluded, all cases, or a specific subset."""

    matched: bool
    cases: tuple[int, ...] | None = None  # None + matched ⇒ all cases

    @classmethod
    def none(cls) -> _CaseMatch:
        return cls(False)

    @classmethod
    def all(cls) -> _CaseMatch:
        return cls(True)


def _match_one(entry: Entry, rx: re.Pattern[str]) -> _CaseMatch:
    """Match a single regex against an entry, resolving family-vs-case granularity."""
    # Plain benchmark, or a family whose own name matches → the whole entry.
    if entry.case_labels is None:
        return _CaseMatch.all() if rx.search(entry.name) else _CaseMatch.none()
    if rx.search(entry.name):
        return _CaseMatch.all()
    hits = sorted({i for i, name in case_names(entry) if rx.search(name)})
    if not hits:
        return _CaseMatch.none()
    if len(hits) == len(entry.case_labels):
        return _CaseMatch.all()
    return _CaseMatch(True, tuple(hits))


def _union(a: _CaseMatch, b: _CaseMatch) -> _CaseMatch:
    if not a.matched:
        return b
    if not b.matched:
        return a
    if a.cases is None or b.cases is None:
        return _CaseMatch.all()
    return _CaseMatch(True, tuple(sorted(set(a.cases) | set(b.cases))))


def _intersect(a: _CaseMatch, b: _CaseMatch) -> _CaseMatch:
    if not a.matched or not b.matched:
        return _CaseMatch.none()
    if a.cases is None:
        return b
    if b.cases is None:
        return a
    both = tuple(sorted(set(a.cases) & set(b.cases)))
    return _CaseMatch(True, both) if both else _CaseMatch.none()


def narrow_entry(
    entry: Entry,
    *,
    any_of: Iterable[re.Pattern[str]] = (),
    all_of: re.Pattern[str] | None = None,
) -> Entry | None:
    """Apply name filters to an entry, returning a (possibly case-narrowed) view or ``None``.

    ``any_of`` is an OR group (the per-path ``file.py::filter`` selectors); ``all_of`` is an
    additional AND constraint (the global ``-k``). A family whose own name matches keeps all
    its cases; otherwise the regexes select individual cases via :func:`case_names`. Returns
    ``None`` when nothing matches, the entry unchanged when all cases survive, or a
    ``dataclasses.replace`` copy with ``cases`` set for a strict subset.
    """
    match = _CaseMatch.all()
    any_of = list(any_of)
    if any_of:
        union = _CaseMatch.none()
        for rx in any_of:
            union = _union(union, _match_one(entry, rx))
        match = _intersect(match, union)
    if all_of is not None:
        match = _intersect(match, _match_one(entry, all_of))
    if not match.matched:
        return None
    return entry if match.cases is None else dataclasses.replace(entry, cases=list(match.cases))


class Registry:
    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._names: set[str] = set()

    def add(self, entry: Entry) -> None:
        # Two entries sharing a name would run as indistinguishable rows and
        # merge into one distribution on compare.
        if entry.name in self._names:
            raise ValueError(
                f"a benchmark named {entry.name!r} is already registered; "
                "pass a unique name= to disambiguate"
            )
        self._names.add(entry.name)
        self._entries.append(entry)

    def clear(self) -> None:
        self._entries.clear()
        self._names.clear()

    def all(self) -> list[Entry]:
        return list(self._entries)

    def filter(
        self,
        pattern: str | None = None,
        *,
        tags: Iterable[str] | None = None,
        literal: bool = False,
    ) -> list[Entry]:
        """Filter by ``pattern`` (a regex matched against the name) and/or ``tags``.

        A ``pattern`` that matches only some cases of a parametrized family
        narrows that family to those cases (see :func:`narrow_entry`). With
        ``literal=True`` the pattern is matched as a fixed string. Tags use OR
        semantics: an entry passes when it has any of the requested tags; entries
        with no tags are excluded whenever ``tags`` is non-empty. Raises
        :class:`ValueError` if ``pattern`` is not a valid regex.
        """
        out = list(self._entries)
        if pattern:
            rx = compile_name_filter(pattern, literal=literal)
            out = [n for e in out if (n := narrow_entry(e, all_of=rx)) is not None]
        if tags:
            wanted = set(tags)
            out = [e for e in out if wanted.intersection(e.tags)]
        return out

    def __len__(self) -> int:
        return len(self._entries)


REGISTRY = Registry()
