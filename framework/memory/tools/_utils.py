"""Shared utilities for scoped file tools."""
from __future__ import annotations

from pathlib import Path


def validate_scoped_path(raw_path: str, allowed_dirs: list[Path]) -> Path:
    """Resolve *raw_path* and verify it falls under an allowed directory.

    Returns the resolved ``Path`` on success; raises ``ValueError`` if the
    path is outside all allowed directories.
    """
    resolved = Path(raw_path).resolve()
    for allowed in allowed_dirs:
        try:
            resolved.relative_to(allowed)
            return resolved
        except ValueError:
            continue
    allowed_str = "\n".join(f"  - {d}" for d in allowed_dirs)
    raise ValueError(
        f"Path '{raw_path}' is outside allowed directories.\n"
        f"Allowed directories:\n{allowed_str}"
    )
