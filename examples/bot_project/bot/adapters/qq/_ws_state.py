"""QQ adapter shared state helpers — channel-workspace persistence + file-type mapping.

Split from ``bot/adapters/qq.py`` (ADR: module-size decomposition). Logic is
unchanged; only the module boundary moved.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path

# QQ rich media file_type: 1=image, 4=file
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

# Persistence file for per-channel current workspace
_CHANNEL_WS_FILE: str = "channel_ws.json"
_REGISTRY_DIR: str = "_registry"


def _qq_file_type(filename: str, is_image: bool | None = None) -> int:
    """Map an outbound file to QQ's rich-media file_type (1=image, 4=file).

    *is_image* (from the Attachment record's magic-byte-derived ``kind``) is
    authoritative when present — no extension guessing. The stdlib
    ``mimetypes`` fallback only covers the legacy path-list case (no record),
    so this adapter carries no hand-maintained extension list.
    """
    if is_image is not None:
        return QQ_FILE_TYPE_IMAGE if is_image else QQ_FILE_TYPE_FILE
    mime, _ = mimetypes.guess_type(filename)
    return QQ_FILE_TYPE_IMAGE if (mime and mime.startswith("image/")) else QQ_FILE_TYPE_FILE


def _read_channel_ws(path: Path, channel_name: str, default: Path) -> Path:
    """Read persisted current_ws for *channel_name* from *path*."""
    if not path.is_file():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        try:
            ws_str = raw.get(channel_name)
        except AttributeError:
            ws_str = None
        if ws_str:
            return Path(ws_str)
    except (json.JSONDecodeError, OSError):
        pass
    return default


def _write_channel_ws(path: Path, data: dict[str, str]) -> None:
    """Atomically write channel_ws mapping to *path*."""
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
