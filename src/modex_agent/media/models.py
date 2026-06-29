"""Attachment domain model — value objects for the attachment system.

Defines the direction-agnostic ``Attachment`` record plus the enums it carries.
See ADR-0013 §3/§8. Frozen value objects throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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


@dataclass(frozen=True)
class Attachment:
    """A file bound to a message, identified by an opaque id.

    Direction-agnostic for rendering; storage differs by ``locator``. This is
    the record persisted in the ServerEvent transcript (the id→path index,
    ADR-0013 §11) — never bytes, only references.
    """

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
            size=int(data["size"]),
            path=str(data["path"]),
            locator=AttachmentLocator(str(data["locator"])),
        )
