"""Recursive deterministic JSON serializer.

Produces a stable byte sequence for any given semantic data, regardless of
dict construction order. Used by :class:`RecordScope.canonical()` and all DB
payload columns requiring deterministic comparison.

Rules:
- Dict keys sorted at every nesting level.
- Sets converted to sorted lists (mixed-type order: None < bool < int/float < str < other).
- Lists and tuples preserve element order; each element is recursively canonicalized.
- ``ensure_ascii=False``, compact separators ``(",", ":")``.
- Non-finite floats (NaN, Infinity, -Infinity) rejected via ``allow_nan=False``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["canonical_json"]


def _sort_key(value: Any) -> tuple[int, Any]:
    """Return a sort key implementing None < bool < int/float < str < other.

    Each category gets a distinct rank so mixed-type sets sort deterministically
    without raising ``TypeError`` on cross-type comparison.
    """
    if value is None:
        return (0, value)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, int | float):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    # For compound types (lists, tuples, dicts) sort by their canonical
    # string representation so ordering is stable and total.
    return (4, _canonicalize(value))


def _canonicalize(data: Any) -> Any:
    """Recursively transform data into a JSON-serializable canonical form."""
    if isinstance(data, Mapping):
        return {key: _canonicalize(data[key]) for key in sorted(data)}
    if isinstance(data, set | frozenset):
        return [_canonicalize(item) for item in sorted(data, key=_sort_key)]
    if isinstance(data, tuple):
        return [_canonicalize(item) for item in data]
    if isinstance(data, list):
        return [_canonicalize(item) for item in data]
    return data


def canonical_json(data: Any) -> str:
    """Serialize *data* to a deterministic JSON string.

    Dict keys are recursively sorted, sets are converted to sorted lists,
    list/tuple element order is preserved with recursive canonicalization,
    and non-finite floats are rejected.
    """
    return json.dumps(
        _canonicalize(data),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=False,
    )
