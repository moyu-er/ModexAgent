"""Core filesystem utilities shared across the framework.

Moved safe_atomic_replace from framework.memory.utils to core to break the
core <-> memory import cycle (core.experience.meta and core.experience.usage
depend on it).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def safe_atomic_replace(tmp_path: Path, target_path: Path) -> None:
    """Replace target with tmp file, with fallback for Windows file-locking.

    On Unix, os.replace is atomic and reliable. On Windows, it can fail
    with PermissionError when the target is held open by another process
    (antivirus, file indexer, concurrent writer). Falls back to a direct write
    in that case.

    Args:
        tmp_path: Temporary file with the new content.
        target_path: Destination file to replace.
    """
    try:
        os.replace(str(tmp_path), str(target_path))
    except OSError:
        content = tmp_path.read_text(encoding="utf-8")
        target_path.write_text(content, encoding="utf-8")
        with contextlib.suppress(OSError):
            tmp_path.unlink()
