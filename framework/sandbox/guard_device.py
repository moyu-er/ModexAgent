"""Device path whitelist for path boundary checks.

Kernel device files that should never be treated as dangerous when
encountered in command-string path extraction.
"""
from __future__ import annotations

BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
})


def is_benign_device_path(path: str) -> bool:
    """Return True if *path* is a benign kernel device file."""
    if path in BENIGN_DEVICE_PATHS:
        return True
    return path.startswith("/dev/fd/")
