"""Media subsystem — concrete attachment storage, MIME classification, gating.

This package owns the concrete media implementation (ADR-0013):
``LocalFileMediaStore`` (filesystem byte storage), ``mime`` magic-byte
sniffing and classification, the ``gate`` perception authority, and the
``security`` dangerous-executable policy. The shared contracts
(``Attachment``, ``MediaStore``, ``StoredFile``, ``StoredMediaKind``,
``MediaRefCollisionError``) live in :mod:`modex_agent.core.media` (plan
§14.1, C1).
"""

from modex_agent.media.store import LocalFileMediaStore

__all__ = [
    "LocalFileMediaStore",
]
