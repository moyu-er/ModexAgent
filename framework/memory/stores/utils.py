"""Storage utilities for cross-platform filesystem safety."""

import hashlib
from pathlib import Path

from pathvalidate import sanitize_filename

MAX_FILENAME_LENGTH = 100


def sanitize_scope_key(scope_key: str) -> str:
    """Sanitize *scope_key* into a filesystem-safe directory name.

    Uses ``pathvalidate.sanitize_filename`` which handles platform-
    specific invalid characters (Windows reserved names, Linux ``/``,
    etc.) and replaces them with ``_``.
    """
    if not scope_key:
        return "_empty_"

    safe = sanitize_filename(scope_key, replacement_text="_")

    if len(safe) <= MAX_FILENAME_LENGTH:
        return safe or "_empty_"

    digest = hashlib.md5(scope_key.encode("utf-8")).hexdigest()[:16]
    truncated = safe[: MAX_FILENAME_LENGTH - len(digest) - 1]
    return f"{truncated}_{digest}"


def ensure_scope_dir(workspace: Path, scope_key: str) -> Path:
    """获取并创建 scope_key 对应的存储目录。"""
    safe_key = sanitize_scope_key(scope_key)
    scope_dir = workspace / safe_key
    scope_dir.mkdir(parents=True, exist_ok=True)
    return scope_dir
