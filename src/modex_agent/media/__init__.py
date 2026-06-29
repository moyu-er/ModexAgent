"""Media subsystem — attachment domain model, MIME classification, storage.

This package is the framework foundation for the attachment system (ADR-0013).
Group 1 ships the pure value-object layer (``models``) plus ``mime`` magic-byte
sniffing and classification. Group 2 adds the storage layer: the
``MediaStore`` ABC, ``LocalFileMediaStore`` backend, and the ``StoredFile``
value object. Gating and ingest land in later groups.
"""

from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.media.store import LocalFileMediaStore, MediaStore, StoredFile

__all__ = [
    "Attachment",
    "AttachmentLocator",
    "Kind",
    "LocalFileMediaStore",
    "MediaStore",
    "StoredFile",
]
