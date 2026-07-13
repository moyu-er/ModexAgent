"""File-lock + append writer for ``modexbot send``.

This module is now a thin typed facade over :mod:`modexctl.main`.
The cross-process lock and append logic live in the production CLI so
that the JSONL writer and locking surface have a single implementation.
"""

from __future__ import annotations

from pathlib import Path

from modexctl.main import _write_line as _modexctl_write_line


def _write_line(
    target_pool_dir: Path,
    target_sid: str,
    line: str,
) -> None:
    """Append one JSONL ``line`` to ``target_sid``'s ``pending.jsonl``.

    Delegates to :func:`modexctl.main._write_line`, which acquires a
    cross-process :class:`filelock.FileLock` and appends the line exactly
    as provided. Directory naming and lock placement are the same as the
    inbox server uses, so the written file is discoverable by
    :class:`LocalFileInboxServer`.

    Args:
        target_pool_dir: Absolute or relative path to the receiver
            pool's inbox root (i.e. ``<inbox_root>/<pool_name>``).
        target_sid: The receiver's session id (used both for the
            directory name and for byte-equal round-trip).
        line: A complete JSONL record (already terminated by the caller).
    """
    _modexctl_write_line(target_pool_dir, target_sid, line)


__all__ = ["_write_line"]
