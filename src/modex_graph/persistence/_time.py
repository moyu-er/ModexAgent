"""Persistence timestamp helpers."""

import time


def now_ms() -> int:
    """Return current epoch time in milliseconds."""
    return int(time.time() * 1000)
