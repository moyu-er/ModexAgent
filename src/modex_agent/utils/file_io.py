"""Atomic file writes and encoding-resilient JSON / JSONL readers.

Atomic replace (plan §15 A2): ``safe_atomic_replace`` is the single canonical
crash-safe rename helper for every file-based writer in the framework (memory
stores, experience/skills metadata, session indexes). It moved here from
``core/utils.py``, which moved it from ``memory/utils.py`` to break the old
core↔memory cycle — the cycle is gone, so both duplicates converged here.
``atomic_write_text`` (write-tmp + ``os.replace``, from
``core/session_store.py``) rides the same consolidation.

All consumers that read JSON or JSONL files from disk should use
``read_json_robust`` and ``read_jsonl_robust`` instead of calling
``Path.read_text(encoding="utf-8")`` or ``Path.open(encoding="utf-8")``
directly.  These helpers automatically fall back through a chain of
common encodings and back up the file if it is unrecoverable.

Encoding priority:  UTF-8 → gb18030 → gbk → gb2312 → latin-1

``latin-1`` decodes *every* byte sequence (it maps bytes 0x00–0xFF to
U+0000–U+00FF), so it is the terminal fallback.  When even latin-1
produces zero valid JSON records the file is treated as corrupt and
backed up.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ordered from most specific to least specific.
# gb18030 is a strict superset of gbk/gb2312.
# latin-1 decodes *all* byte sequences — it never raises UnicodeDecodeError.
_FALLBACK_ENCODINGS: tuple[str, ...] = ("gb18030", "gbk", "gb2312", "latin-1")


# ── Atomic writes ─────────────────────────────────────────────────────────


def safe_atomic_replace(tmp_path: Path, target_path: Path) -> None:
    """Replace target with tmp file, with fallback for Windows file-locking.

    On Unix, ``os.replace`` is atomic and reliable. On Windows, it can fail
    with ``PermissionError`` when the target is held open by another process
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


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically via a temp file + ``os.replace``.

    The target is never observed in a partially-written state: either the
    previous content remains (if the final replace fails) or the new content
    is fully in place.  The temp file is cleaned up on failure.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# ── Robust readers ────────────────────────────────────────────────────────


def read_json_robust(path: Path) -> dict[str, Any] | None:
    """Read a single JSON object from *path* with encoding fallback.

    Returns:
        Parsed ``dict`` on success.
        ``None`` if *path* does not exist.
        ``{}`` if *path* exists but is unrecoverable (backed up to ``.bak``).
    """
    if not path.exists():
        return None

    for encoding in _encodings():
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError), path.open(
            encoding=encoding
        ) as fh:
            text = fh.read().strip()
            if not text:
                return {}
            result = json.loads(text)
            if encoding != "utf-8":
                _rewrite_as_utf8(path, result, encoding)
            return result

    # Unrecoverable
    _backup(path, "JSON")
    return {}


def read_jsonl_robust(path: Path) -> list[dict[str, Any]]:
    """Read JSONL from *path* with encoding fallback and per-line resilience.

    * Individual unparseable lines are **skipped** with a warning.
    * If **all** lines fail to parse (and the file is non-empty) the file
      is treated as corrupt and backed up to ``.bak``.
    * On success with a non-UTF-8 encoding, the file is **auto-rewritten**
      in UTF-8 so the problem heals itself.

    Returns:
        Parsed ``list[dict]`` on success, ``[]`` if missing or unrecoverable.
    """
    if not path.exists():
        return []

    file_size = path.stat().st_size
    for encoding in _encodings():
        with contextlib.suppress(UnicodeDecodeError):
            messages = _parse_jsonl_lines(path, encoding)
            if messages or file_size == 0:
                if encoding != "utf-8":
                    logger.warning(
                        "%s was encoded as %s (expected UTF-8).  "
                        "Data recovered and re-encoded as UTF-8.",
                        path,
                        encoding,
                    )
                    _rewrite_jsonl(path, messages)
                return messages
            # Decoded but zero valid lines → corrupt
            logger.warning(
                "%s decoded as %s but contained no valid JSON lines.  Treating as corrupted.",
                path,
                encoding,
            )
            break

    # Unrecoverable
    _backup(path, "JSONL")
    return []


# ── Internal helpers ──────────────────────────────────────────────────────


def _encodings():
    """Yield ``'utf-8'`` first, then the fallback chain."""
    yield "utf-8"
    yield from _FALLBACK_ENCODINGS


def _parse_jsonl_lines(path: Path, encoding: str) -> list[dict[str, Any]]:
    """Parse JSONL lines, skipping unparseable ones with a warning."""
    messages: list[dict[str, Any]] = []
    with path.open(encoding=encoding) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping unparseable line in %s (encoding=%s): %.80r",
                    path,
                    encoding,
                    line[:200],
                )
    return messages


def _rewrite_as_utf8(path: Path, data: dict[str, Any], source_encoding: str) -> None:
    """Rewrite *path* in UTF-8 after recovering from *source_encoding*."""
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Re-encoded %s from %s to UTF-8.", path, source_encoding)
    except OSError:
        logger.debug("Could not re-encode %s (non-critical).", path)


def _rewrite_jsonl(path: Path, messages: list[dict[str, Any]]) -> None:
    """Rewrite *path* as UTF-8 JSONL."""
    try:
        with path.open("w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("Could not re-encode %s (non-critical).", path)


def _backup(path: Path, kind: str) -> None:
    """Move *path* to ``.bak`` and log an error."""
    backup_path = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.move(str(path), str(backup_path))
        logger.error(
            "%s %s is corrupted (cannot decode with any encoding).  Backed up to %s.",
            kind,
            path,
            backup_path,
        )
    except OSError as exc:
        logger.error(
            "Cannot back up corrupted %s: %s.  Removing file.",
            path,
            exc,
        )
        with contextlib.suppress(OSError):
            path.unlink()
