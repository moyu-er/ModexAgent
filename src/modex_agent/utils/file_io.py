"""Encoding-resilient JSON / JSONL file readers.

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
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ordered from most specific to least specific.
# gb18030 is a strict superset of gbk/gb2312.
# latin-1 decodes *all* byte sequences — it never raises UnicodeDecodeError.
_FALLBACK_ENCODINGS: tuple[str, ...] = ("gb18030", "gbk", "gb2312", "latin-1")


# ── Public API ────────────────────────────────────────────────────────────


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
        with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
            with path.open(encoding=encoding) as fh:
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
