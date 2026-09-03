"""Media contracts — attachment values and the MediaStore ABC (ADR-0013).

The shared seam of the attachment system, promoted to core (plan §14.1, C1):
the direction-agnostic ``Attachment`` record, the stored-media kinds and
values, and the backend-swappable ``MediaStore`` contract. Concrete
filesystem behavior stays in :mod:`modex_agent.media.store`
(``LocalFileMediaStore``); MIME classification, the perception gate, and the
security policy stay in :mod:`modex_agent.media`.

MediaStore is an explicit retained seam (ADR-0013 §6): backend-swappable
inbound media storage with messages/tools/business resolvers crossing it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict


class Kind(StrEnum):
    """Three-way classification of an attachment by its authoritative MIME.

    Computed once at ingest from magic-byte MIME (ADR-0013 §8) and stored on
    the Attachment record. ``image`` and ``extractable_document`` are the
    gate-accepted kinds; ``other`` is everything else.
    """

    IMAGE = "image"
    EXTRACTABLE_DOCUMENT = "extractable_document"
    OTHER = "other"


class AttachmentLocator(StrEnum):
    """Where an attachment's bytes physically live — a read-dispatch switch.

    ``media``: inbound upload persisted under the managed media dir; ``path`` is
    relative to the workspace root. ``workspace``: outbound agent-produced file
    kept in place; ``path`` is the literal absolute path the agent gave. This is
    an internal concern of the download path and is invisible to the frontend
    (ADR-0013 §3/§4).
    """

    MEDIA = "media"
    WORKSPACE = "workspace"


class Attachment(BaseModel):
    """A file bound to a message, identified by an opaque id.

    Direction-agnostic for rendering; storage differs by ``locator``. This is
    the record persisted in the ServerEvent transcript (the id→path index,
    ADR-0013 §11) — never bytes, only references.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: Kind
    name: str
    mime: str | None
    size: int
    path: str
    locator: AttachmentLocator

    @property
    def is_image(self) -> bool:
        """Whether this record renders as an image (vs a file card/download).

        The single image-vs-file switch every channel renderer needs (IM inline
        render, webui card kind). Derived from :attr:`kind`, which is computed
        at ingest from magic bytes — never from the filename extension — so
        channels need no extension lists of their own (ADR-0013 §8).
        """
        return self.kind is Kind.IMAGE

    def to_dict(self) -> dict[str, object]:
        """Serialize to the metadata-only dict stored in transcript events.

        Round-trips via :meth:`from_dict`. Stores ONLY metadata
        (id/kind/name/mime/size/path/locator) — never bytes (ADR-0013 §11).
        ``Kind``/``AttachmentLocator`` are ``StrEnum``, so their ``str`` value
        is the wire form.
        """
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "path": self.path,
            "locator": self.locator.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Attachment:
        """Inverse of :meth:`to_dict` — rebuild the VO from a serialized dict."""
        return cls(
            id=str(data["id"]),
            kind=Kind(str(data["kind"])),
            name=str(data["name"]),
            mime=(str(data["mime"]) if data.get("mime") is not None else None),
            size=int(str(data["size"])),
            path=str(data["path"]),
            locator=AttachmentLocator(str(data["locator"])),
        )


class StoredMediaKind(StrEnum):
    """Closed set of persisted media subtrees."""

    UPLOADS = "uploads"
    READS = "reads"


class MediaRefCollisionError(Exception):
    """The same media reference exists in both persisted subtrees."""

    session_id: str
    attachment_id: str

    def __init__(self, session_id: str, attachment_id: str) -> None:
        self.session_id = session_id
        self.attachment_id = attachment_id
        super().__init__(
            f"media reference {attachment_id!r} for session {session_id!r} "
            "exists in both uploads and reads"
        )


class StoredFile(BaseModel):
    """One persisted attachment file under a session's uploads directory.

    Frozen value object — a snapshot of the on-disk entry at listing time.
    Cross-seam value shared by the store contract and its callers; ``path``
    is the absolute on-disk location, so it is typed as an arbitrary
    filesystem path.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

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
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path:
        """Persist ``stream`` under ``<kind>/<session_id>/<attachment_id>``.

        ``stream`` is a readable binary file-like object or a ``bytes``
        blob; both are written without buffering the whole payload in memory.
        Returns the absolute path of the stored file.
        """

    @abstractmethod
    def read(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> Path | None:
        """Return the stored path, or ``None`` if no such file exists.

        Does NOT read bytes into memory — the caller streams the path.
        """

    @abstractmethod
    def read_bytes(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> bytes | None:
        """Return bytes from one explicit subtree, or ``None`` when absent."""

    @abstractmethod
    def resolve_bytes(self, session_id: str, attachment_id: str) -> bytes | None:
        """Resolve bytes across uploads then reads, rejecting collisions."""

    @abstractmethod
    def delete(
        self,
        session_id: str,
        attachment_id: str,
        *,
        kind: StoredMediaKind = StoredMediaKind.UPLOADS,
    ) -> bool:
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
