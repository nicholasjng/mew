"""User-defined benchmark context.

A process-global ``dict[str, Any]`` populated via :func:`set_context` and
:func:`update_context`. Dotted keys nest, so ``set_context("dataset.size", 1024)``
yields ``{"dataset": {"size": 1024}}``. :func:`mew.run` stores a snapshot under
the reporter's ``context`` key.
"""

from __future__ import annotations

import copy
from typing import Any

_CONTEXT: dict[str, Any] = {}


def _check_key(key: str) -> list[str]:
    if not isinstance(key, str) or not key:
        raise ValueError(f"context key must be a non-empty string, got {key!r}")
    parts = key.split(".")
    if any(not p for p in parts):
        raise ValueError(f"context key has empty path segment: {key!r}")
    return parts


def _set_nested(key: str, value: Any) -> None:
    parts = _check_key(key)
    cur: dict[str, Any] = _CONTEXT
    for i, part in enumerate(parts[:-1]):
        cur = cur.setdefault(part, {})
        if isinstance(cur, dict):
            continue
        path = ".".join(parts[: i + 1])
        raise ValueError(
            f"cannot set context key {key!r}: existing value at {path!r} "
            f"is not a dict ({type(cur).__name__})"
        )
    cur[parts[-1]] = value


def set_context(key: str, value: Any) -> None:
    """Set one value in the context that every run records.

    Parameters
    ----------
    key : str
        Context key. Dotted names nest (``"dataset.size"`` → ``{"dataset": {"size": ...}}``).
    value : Any
        Stored as is; file reporters write it as JSON, falling back to ``str()``.

    Raises
    ------
    ValueError
        If ``key`` is empty, has an empty path segment, or traverses through a non-dict value.
    """
    _set_nested(key, value)


def update_context(*mapping: dict[str, Any], **kwargs: Any) -> None:
    """Set several context values: the mappings in order, then ``kwargs``.

    Dotted keys nest in both forms; pass them as keywords with
    ``update_context(**{"a.b": 1})``.

    Parameters
    ----------
    *mapping : dict[str, Any]
        Mappings to apply, in order.
    **kwargs
        Values applied after the mappings.

    Raises
    ------
    ValueError
        Same conditions as :func:`set_context`.
    """
    for m in (*mapping, kwargs):
        for k, v in m.items():
            _set_nested(k, v)


def get_context() -> dict[str, Any]:
    """Return a deep copy of the context.

    Returns
    -------
    dict[str, Any]
        A copy; changing it doesn't change what runs record.
    """
    return copy.deepcopy(_CONTEXT)


def clear_context() -> None:
    """Remove every context value."""
    _CONTEXT.clear()


__all__ = [
    "clear_context",
    "get_context",
    "set_context",
    "update_context",
]
