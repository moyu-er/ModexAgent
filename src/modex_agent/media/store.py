"""MediaStore — framework byte store for inbound attachments (ADR-0013 §6).

A reusable, backend-swappable byte store. ``LocalFileMediaStore`` operates
purely on the directory it is given (the already-resolved pool media root);
it has NO workspace/pool/ws knowledge — that routing is the business
resolver's job (``bot.service.media_store``), mirroring
``WorkspaceScopedTranscriptStore``.

Stream/path-oriented throughout: ``save`` accepts a binary stream (or bytes)
and copies it in fixed-size chunks; ``read`` returns the ``Path`` so the caller
streams the response. The largest configured file never buffers whole into
memory.

On-disk layout::

    <media_dir>/uploads/<session_id>/<attachment_id>

``session_id`` and ``attachment_id`` are sanitized via :func:`safe_segment`
so neither can escape ``<media_dir>`` via ``..``.
"""

from __future__ import annotations

import contextlib
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from modex_agent.workspace.paths import safe_segment

# Chunk size for streamed copies. Small enough to keep memory flat for the
# largest configured file (1 GB outbound), large enough to amortize per-call
# overhead. Reading/writing in a fixed-size loop is what makes ``save`` a
# streaming operation rather than a whole-file buffer.
_CHUNK_BYTES: int = 64 * 1024

# Subdirectory under the media dir holding all uploaded attachment bytes.
# Kept as a leaf so future sibling leaves (e.g. derived thumbnails) can be
# added without touching the path of existing uploads.
_UPLOADS_SUBDIR: str = "uploads"


@dataclass(frozen=True)
class StoredFile:
    """One persisted attachment file under a session's uploads directory.

    Frozen value object — a snapshot of the on-disk entry at listing time.
    """

    attachment_id: str
    path: Path
    size: int
    mtime: float


class MediaStore(ABC):
    """Framework ABC for a backend-swappable inbound byte store.

    Implementations receive an already-resolved media directory (the business
    resolver maps ``ws``+``pool`` to a directory and hands each (ws,pool) a
    cached store). The ABC therefore deals only in ``session_id`` /
    ``attachment_id`` keys.
    """

    @abstractmethod
    def save(
        self,
        session_id: str,
        attachment_id: str,
        stream: BinaryIO | bytes,
    ) -> Path:
        """Persist ``stream`` under ``<session_id>/<attachment_id>``.

        ``stream`` is a readable binary file-like object or a ``bytes``
        blob; both are written without buffering the whole payload in memory.
        Returns the absolute path of the stored file.
        """

    @abstractmethod
    def read(self, session_id: str, attachment_id: str) -> Path | None:
        """Return the stored path, or ``None`` if no such file exists.

        Does NOT read bytes into memory — the caller streams the path.
        """

    @abstractmethod
    def delete(self, session_id: str, attachment_id: str) -> bool:
        """Delete the stored file. Returns ``True`` if a file was removed."""

    @abstractmethod
    def list_session(self, session_id: str) -> list[StoredFile]:
        """List stored files for a session, ordered by attachment_id."""

    @abstractmethod
    def enforce_budget(self, session_id: str, budget_bytes: int) -> list[Path]:
        """Evict oldest-by-mtime files until the session total ≤ ``budget_bytes``.

        Returns the paths of evicted files (empty when already within budget).
        See ADR-0013 §7 Layer 2.

        Tie-break: when two files share an mtime, the one whose
        ``attachment_id`` sorts earlier is evicted first — ``list_session``
        orders by ``attachment_id`` and the mtime sort is stable.
        """


class LocalFileMediaStore(MediaStore):
    """Local-filesystem ``MediaStore`` rooted at a resolved media directory.

    The store never learns the workspace root or pool name — it owns only the
    ``uploads/<session_id>/<attachment_id>`` subtree under ``media_dir``.

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

    def _uploads_root(self) -> Path:
        return self._media_dir / _UPLOADS_SUBDIR

    def _session_dir(self, session_id: str) -> Path:
        """The uploads subtree for one session (created on demand)."""
        return self._uploads_root() / safe_segment(session_id)

    def _file_path(self, session_id: str, attachment_id: str) -> Path:
        """Absolute path for one stored file. Path-escape-proof via safe_segment."""
        return self._session_dir(session_id) / safe_segment(attachment_id)

    # ------------------------------------------------------------------
    # MediaStore interface
    # ------------------------------------------------------------------

    def save(
        self,
        session_id: str,
        attachment_id: str,
        stream: BinaryIO | bytes,
    ) -> Path:
        target = self._file_path(session_id, attachment_id)
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

    def read(self, session_id: str, attachment_id: str) -> Path | None:
        target = self._file_path(session_id, attachment_id)
        return target if target.is_file() else None

    def delete(self, session_id: str, attachment_id: str) -> bool:
        target = self._file_path(session_id, attachment_id)
        if not target.is_file():
            return False
        target.unlink()
        # Drop the session dir when it becomes empty so list/enforce scans stay
        # cheap and the uploads tree reflects reality.
        session_dir = self._session_dir(session_id)
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
