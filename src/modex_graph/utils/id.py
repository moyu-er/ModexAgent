"""Sortable short identifier generation."""

from __future__ import annotations

import secrets
import time
from typing import Final

_EPOCH_SECONDS: Final = 1_704_067_200.0
_RANDOM_ALPHABET: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_RANDOM_LENGTH: Final = 14


def generate_id(prefix: str | None = None, separator: str = "_") -> str:
    """Generate a 26-character time-sortable identifier with an optional prefix."""
    elapsed_milliseconds = int((time.time() - _EPOCH_SECONDS) * 1_000)
    timestamp = f"{elapsed_milliseconds:012x}"
    random_suffix = "".join(secrets.choice(_RANDOM_ALPHABET) for _ in range(_RANDOM_LENGTH))
    body = f"{timestamp}{random_suffix}"
    normalized_prefix = prefix.strip() if prefix is not None else ""
    return f"{normalized_prefix}{separator}{body}" if normalized_prefix else body


__all__ = ["generate_id"]
