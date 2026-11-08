"""Encode user values for result rows, so a row never holds a ``str()`` of an object.

A value JSON represents as itself (``str``, ``int``, ``float``, ``bool``, ``None``)
is kept; anything else becomes its type's qualified name, such as
``"torch.Tensor"``. A tensor's ``str()`` can run to hundreds of kilobytes and
still not identify it; its type at least says what it was. ``float`` NaN and
infinities are kept and written as JSON's ``NaN`` and ``Infinity`` extensions.
"""

from __future__ import annotations

from typing import Any

_SCALARS = (str, int, float, bool, type(None))


def type_name(value: Any) -> str:
    """``value``'s type as ``module.QualName``, e.g. ``"torch.Tensor"``."""
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def encode_param(value: Any) -> Any:
    """A case parameter as a row holds it: a JSON scalar, or its type's name."""
    return value if isinstance(value, _SCALARS) else type_name(value)
