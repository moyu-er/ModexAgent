"""LocalFileMediaStore — concrete local-filesystem media storage (ADR-0013 §6).

The contracts (``MediaStore`` ABC, ``StoredFile``, ``StoredMediaKind``,
``MediaRefCollisionError``) live in :mod:`modex_agent.core.media` (plan §14.1,
C1); this module owns only the concrete filesystem behavior.

``LocalFileMediaStore`` operates purely on the directory it is given (the
already-resolved pool media root); it has NO workspace/pool/ws knowledge —
that routing is the business resolver's job (``bot.service.media_store``),
mirroring ``WorkspaceScopedTranscriptStore``.

The upload API remains stream/path-oriented: ``save`` accepts a binary stream
(or bytes) and copies it in fixed-size chunks; ``read`` returns the ``Path`` so
the caller streams the response. Explicit byte-reading methods support model
injection, where the payload must be materialized.

On-disk layout::

    <media_dir>/uploads/<session_id>/<attachment_id>
    <media_dir>/reads/<session_id>/<attachment_id>

``session_id`` and ``attachment_id`` are sanitized via :func:`safe_segment`
so neither can escape ``<media_dir>`` via ``..``.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import BinaryIO

from modex_agent.core.media import (
    MediaRefCollisionError,
    MediaStore,
    StoredFile,
    StoredMediaKind,
)
from modex_agent.workspace.paths import safe_segment

# Chunk size for streamed copies. Small enough to keep memory flat for the
# largest configured file (1 GB outbound), large enough to amortize per-call
# overhead. Reading/writing in a fixed-size loop is what makes ``save`` a
# streaming operation rather than a whole-file buffer.
_CHUNK_BYTES: int = 64 * 1024

__all__ = ["LocalFileMediaStore"]


class LocalFileMediaStore(MediaStore):
    """Local-filesystem ``MediaStore`` rooted at a resolved media directory.

    The store never learns the workspace root or pool name — it owns only the
    ``<kind>/<session_id>/<attachment_id>`` subtrees under ``media_dir``.

    Concurrency: writes are atomic (a ``.part`` temp file then ``replace``),
    but concurrent writers to the same ``(session_id, attachment_id)`` race —
    last writer wins, no corruption. ``attachment_id`` is caller-unique so this
    is not expected in practice.
    """

    def __init__(self, media_dir: Path) -> None:
        self._media_dir: Path = Path(media_dir)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @property
    def media_dir(self) -> Path:
        """The resolved media root this store writes under."""
        return self._media_dir

    def _session_dir(
        self,
        session_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path:
        """One media-kind subtree for a session, created on demand by save."""
        return self._media_dir / kind.value / safe_segment(session_id)

    def _file_path(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path:
        """Absolute path for one stored file. Path-escape-proof via safe_segment."""
        return self._session_dir(session_id, kind=kind) / safe_segment(attachment_id)

    # ------------------------------------------------------------------
    # MediaStore interface
    # ------------------------------------------------------------------

    def save(
        self,
        session_id: str,
        attachment_id: str,
        stream: BinaryIO | bytes,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path:
        target = self._file_path(session_id, attachment_id, kind=kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write a sibling temp file then replace, so a crash mid-write never
        # leaves a half-written attachment under the live id.
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            if isinstance(stream, bytes | bytearray):
                # ``bytes`` is already in memory; write it directly without an
                # intermediate buffer. The whole-file-buffering concern is about
                # streams of unknown size, not already-materialized bytes.
                tmp.write_bytes(stream)
            else:
                with tmp.open("wb") as out:
                    shutil.copyfileobj(stream, out, length=_CHUNK_BYTES)
            tmp.replace(target)
        except BaseException:
            # Clean up the partial file on any failure; never leave .part behind.
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
            raise
        return target

    def read(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path | None:
        target = self._file_path(session_id, attachment_id, kind=kind)
        return target if target.is_file() else None

    def read_bytes(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> bytes | None:
        target = self.read(session_id, attachment_id, kind=kind)
        return target.read_bytes() if target is not None else None

    def resolve_bytes(self, session_id: str, attachment_id: str) -> bytes | None:
        upload = self.read(session_id, attachment_id, kind=StoredMediaKind.UPLOADS)
        read_snapshot = self.read(session_id, attachment_id, kind=StoredMediaKind.READS)
        if upload is not None and read_snapshot is not None:
            raise MediaRefCollisionError(session_id, attachment_id)
        target = upload if upload is not None else read_snapshot
        return target.read_bytes() if target is not None else None

    def delete(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> bool:
        target = self._file_path(session_id, attachment_id, kind=kind)
        if not target.is_file():
            return False
        target.unlink()
        # Drop the session dir when it becomes empty so list/enforce scans stay
        # cheap and the selected media-kind tree reflects reality.
        session_dir = self._session_dir(session_id, kind=kind)
        # Not empty (other attachments remain) — expected, not an error.
        with contextlib.suppress(OSError):
            session_dir.rmdir()
        return True

    def list_session(self, session_id: str) -> list[StoredFile]:
        session_dir = self._session_dir(session_id)
        if not session_dir.is_dir():
            return []
        entries: list[StoredFile] = []
        for p in session_dir.iterdir():
            if not p.is_file():
                continue
            stat = p.stat()
            entries.append(
                StoredFile(
                    attachment_id=p.name,
                    path=p,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        entries.sort(key=lambda e: e.attachment_id)
        return entries

    def enforce_budget(self, session_id: str, budget_bytes: int) -> list[Path]:
        files = self.list_session(session_id)
        total = sum(f.size for f in files)
        if total <= budget_bytes:
            return []
        # Evict oldest by mtime until within budget.
        by_oldest = sorted(files, key=lambda f: f.mtime)
        evicted: list[Path] = []
        for entry in by_oldest:
            if total <= budget_bytes:
                break
            try:
                entry.path.unlink()
            except OSError:
                # File vanished between list and unlink — treat as not-ours.
                continue
            evicted.append(entry.path)
            total -= entry.size
        # Drop the session dir if now empty.
        session_dir = self._session_dir(session_id)
        with contextlib.suppress(OSError):
            session_dir.rmdir()
        return evicted
