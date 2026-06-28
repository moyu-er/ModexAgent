"""Generic user-input envelope shared by all channels before processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AttachmentRef:
    """Structured reference to a channel attachment (image/file/...)."""

    url: str | None = None
    filename: str | None = None
    local_path: str | None = None
    mime_type: str | None = None


@dataclass
class UserInputEnvelope:
    """Normalized user input carried through the input pipeline.

    external_id: raw external session identifier (the session id prefix seed).
                 WebUI=uuid_prefix, IM=user_id. Fed verbatim to SessionIdFactory
                 via ``encode_snowflake`` to form the session id prefix; pool
                 isolation is keyed by that prefix, so channels do not cross.
    content:     raw user content (original form, incl. /skillName ...).
    channel:     channel name provided by the adapter (not hardcoded).
    explicit_pool: pool chosen by the UI (WebUI); None for IM.
    metadata:    cross-stage scratch + raw channel metadata.
    attachments: structured attachments.
    pre_resolved_session: a SessionInfo already established upstream
                 (e.g. WebUI created it during attach). When set, the
                 pipeline uses str(this) as the canonical key and does
                 NOT re-encode external_id — preventing double
                 encoding. None for IM, where the pipeline resolves
                 once via SessionIdFactory.create(external_id=...).
    command_resolved: set True by any stage that claims this envelope's
                 slash command. The terminal UnsupportedCommandStage
                 rejects a "/command" only when this is still False, so
                 no stage needs to know which other stage claimed it.
    """

    external_id: str
    content: str
    channel: str
    explicit_pool: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)
    pre_resolved_session: Any = None
    command_resolved: bool = False