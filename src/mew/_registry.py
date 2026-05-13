"""Module-level registry that @benchmark / @parametrize populate.

The registry is process-global so benchmark files imported from anywhere
(pytest-style discovery, manual import, REPL) accumulate into one place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Entry:
    name: str
    fn: Callable[..., None]
    module: str | None = None
    file: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


class Registry:
    def __init__(self) -> None:
        self._entries: list[Entry] = []

    def add(self, entry: Entry) -> None:
        self._entries.append(entry)

    def clear(self) -> None:
        self._entries.clear()

    def all(self) -> list[Entry]:
        return list(self._entries)

    def filter(
        self,
        pattern: str | None = None,
        *,
        tags: Iterable[str] | None = None,
    ) -> list[Entry]:
        """Filter by `pattern` (pytest-style `-k` substring) and/or `tags`.

        Tags use OR semantics: an entry passes when it has any of the
        requested tags. An entry with no tags is always excluded when `tags`
        is non-empty.
        """
        out = list(self._entries)
        if pattern:
            out = [e for e in out if pattern in e.name]
        if tags:
            wanted = set(tags)
            out = [e for e in out if wanted.intersection(e.tags)]
        return out

    def __len__(self) -> int:
        return len(self._entries)


REGISTRY = Registry()
