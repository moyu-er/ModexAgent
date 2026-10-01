"""Generic user-input envelope shared by all channels before processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.media import Attachment


class CommandStatus(StrEnum):
    """Lifecycle of a slash command as it flows through the pipeline.

    UNRESOLVED — no stage has claimed the command yet. The terminal
        UnsupportedCommandStage rejects any "/command" still in this state.
    RESOLVED   — a stage claimed it and the pipeline should continue normally
        (persist the user message, enqueue to the agent). Set by SkillParseStage
        and ApprovalStage.
    HANDLED    — a stage claimed it and fully handled it (e.g. enqueued a
        continue signal, switched workspace). Downstream stages skip persist
        and enqueue. Set by CommandDispatchStage, EnvironmentControlStage,
        SessionControlStage.
    """

    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    HANDLED = "handled"


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
    command_status: lifecycle of this envelope's slash command, carried as
                 a ``CommandStatus`` enum (UNRESOLVED / RESOLVED / HANDLED).
                 Starts UNRESOLVED; a claiming stage sets it to RESOLVED
                 (pipeline continues normally — persist + enqueue) or
                 HANDLED (fully processed — persist + enqueue skipped).
                 The terminal UnsupportedCommandStage rejects a "/command"
                 only when this is still UNRESOLVED, so no stage needs to
                 know which other stage claimed it.
    """

    external_id: str
    content: str
    channel: str
    explicit_pool: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)
    pre_resolved_session: Any = None
    command_status: CommandStatus = CommandStatus.UNRESOLVED
    """Lifecycle of the envelope's slash command. Set by claiming stages;
    consumed by UnsupportedCommandStage (reject if UNRESOLVED),
    PersistUserMessageStage (skip if HANDLED), and EnqueueStage (skip if
    HANDLED). See ``CommandStatus`` for the state contract."""
    resolved_attachments: list[Attachment] = field(default_factory=list)
    """Gate-accepted, persisted inbound attachments for THIS turn, produced by
    the attachment ingest stage. Typed handoff to the transcript-write stage
    (G4) and the agent-perception injection (G5) — never bytes, only records.
    Empty when no attachment was accepted. Outbound attachments are NOT listed
    here (they are produced by SendFileToUserTool, not the input pipeline)."""
