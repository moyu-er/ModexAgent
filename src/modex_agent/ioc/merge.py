"""Deep merge utility for configuration inheritance."""

from __future__ import annotations

from collections.abc import Mapping


def deep_merge(
    base: dict[str, object],
    override: dict[str, object] | None,
) -> dict[str, object]:
    """Deep merge two dicts. Lists are replaced, None clears the key.

    Args:
        base: The base dict providing defaults.
        override: The override dict. None values explicitly clear keys.

    Returns:
        A new merged dict. base is never mutated.
    """
    if override is None:
        return {**base}

    result: dict[str, object] = {}
    all_keys = set(base.keys()) | set(override.keys())

    for key in all_keys:
        if key in override:
            val = override[key]
            if val is None:
                continue
            if isinstance(val, Mapping) and isinstance(base.get(key), Mapping):
                result[key] = deep_merge(
                    dict(base[key]),  # type: ignore[arg-type, call-overload]
                    dict(val),  # type: ignore[arg-type, call-overload]
                )
            else:
                result[key] = val
        else:
            result[key] = base[key]
    return result
