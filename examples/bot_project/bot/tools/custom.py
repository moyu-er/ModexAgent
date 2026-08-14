"""Custom tools for the bot project.

Contains user-facing tools (SendFileToUserTool).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from pathlib import Path

from bot.webui.transcript_store import TranscriptStore
from modex_agent.core.session_id import agent_of
from modex_agent.core.tool_manager import (
    Tool,
    ToolConfig,
)
from modex_agent.media.mime import classify_kind, sniff_mime
from modex_agent.media.models import Attachment, AttachmentLocator
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.workspace.runtime import resolve_workspace_root

logger = logging.getLogger(__name__)


class SendFileToUserTool(Tool):
    """Send a local file to the current user.

    Outbound attachment producer (ADR-0013 §3/§4/§7). The file stays in place
    (no copy); an :class:`Attachment` record with ``locator=WORKSPACE`` and the
    literal absolute ``path`` is persisted to the transcript so the download
    endpoint (G6) can resolve it via :func:`find_attachment` (G4). Outbound does
    NOT pass the perception gate — the agent produced the file deliberately —
    only the ``MediaConfig.max_outbound_bytes`` cap applies.
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        *,
        transcript_store: TranscriptStore | None = None,
        media_config: MediaConfig | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
    ) -> None:
        self._output_adapter = output_adapter
        # None disables persistence (legacy wiring / tests without a store).
        self._transcript_store = transcript_store
        self._media_config = media_config or MediaConfig()
        # Resolver-cell-driven workspace sessions dir, mirroring the emitter's
        # provider so the outbound record lands in the owning workspace's
        # transcript (survives the broker-queue task boundary).
        self._sessions_dir_provider = sessions_dir_provider
        super().__init__(
            name="send_file_to_user",
            description=(
                "Send a file from the local filesystem to the current user. "
                "Use this tool when you have created or found a file that the user should receive. "
                "The file path can be absolute or relative to the working directory. "
                "You may include a short message explaining what the file contains."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to send (absolute or relative to working directory).",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional accompanying message to send with the file.",
                        "default": "",
                    },
                },
                "required": ["file_path"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        file_path = kwargs.get("file_path", "")
        message = kwargs.get("message", "")

        if not file_path:
            return "Error: file_path is required."

        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = resolve_workspace_root() / path
        path = path.resolve()

        if not path.exists():
            return f"Error: File not found: {file_path}"
        if not path.is_file():
            return f"Error: Not a regular file: {file_path}"

        from modex_agent.core.agent import current_agent_context
        from modex_agent.core.types import OutputMessage

        agent_ctx = current_agent_context.get(None)
        if agent_ctx is None:
            return "Error: No active agent context. Cannot send file."
        session_id = agent_ctx.session.session_id
        if not session_id:
            return "Error: No session_id in agent context. Cannot send file."

        size = path.stat().st_size

        # Outbound cap (ADR-0013 §7): hard reject over the configured limit. The
        # outbound path does NOT run the perception gate — any type/path is
        # accepted up to the cap.
        if size > self._media_config.max_outbound_bytes:
            limit = self._media_config.max_outbound_bytes
            return (
                f"Error: File is too large to send ({size} bytes; limit is "
                f"{limit} bytes). Use a smaller file."
            )

        # Magic-byte MIME is authoritative; kind is the three-way classification
        # (ADR-0013 §8). Read only the bytes sniff_mime needs.
        with path.open("rb") as f:
            head = f.read(64)
        mime = sniff_mime(head, path.name)
        kind = classify_kind(mime)

        attachment = Attachment(
            id=uuid.uuid4().hex,
            kind=kind,
            name=path.name,
            mime=mime,
            size=size,
            path=str(path),
            locator=AttachmentLocator.WORKSPACE,
        )

        # Persist the outbound record BEFORE sending the card so the download
        # endpoint (G6) can resolve it via find_attachment (G4) the moment a
        # client receives the attachment-card delta. Persist is best-effort
        # (_persist_attachment swallows its own errors) so it cannot break the
        # send below. The record rides on an assistant_turn ServerEvent
        # (ADR-0013 §11), scanned by find_attachment alongside user_message
        # inbound records.
        await self._persist_attachment(session_id, attachment)

        try:
            # Deliver on the CURRENT turn's channel adapter, not the adapter
            # captured at pool-build time. A pool shared across channels (or an
            # IM turn on a pool whose fixed adapter is QQ) would otherwise route
            # a webui-bound file to IM. The turn's adapter is the one bound to
            # this turn's emitter (agent.py sets context.emitter for the ReAct
            # loop); fall back to the fixed adapter only when no emitter is
            # bound (legacy/test wiring).
            emitter = agent_ctx.emitter
            output_adapter = (
                getattr(emitter, "output_adapter", None) or self._output_adapter
            )
            await output_adapter.send(
                OutputMessage(
                    content=message,
                    attachments=[str(path)],
                    # Carry the built record so the adapter can emit an
                    # attachment-card delta with the attachment_id (the path
                    # list alone cannot build the download URL).
                    attachment_records=[attachment],
                ),
                session_id,
            )
        except Exception as e:
            return f"Error sending file: {e}"

        # NOTE: do NOT call ``agent_ctx.add_attachment``. This tool self-delivers
        # (the direct send above is the single unified path for webui and IM);
        # registering the path would make the emitter's ``emit_complete`` re-send
        # ``result.attachments`` and the user would receive the file twice.
        return f"File sent successfully: {path.name}"

    async def _persist_attachment(
        self, session_id: str, attachment: Attachment
    ) -> None:
        """Record the outbound Attachment on an assistant_turn transcript event.

        Best-effort: a transcript write failure must not break the send (the
        file already reached the user via the adapter). The store's Resilient
        decorator already swallows OSError on append; this guard covers the
        no-store-wired case (tests / legacy wiring) and unexpected errors.
        """
        if self._transcript_store is None:
            return
        try:
            from bot.webui.events import AssistantTurnEvent

            event = AssistantTurnEvent(
                session_id=session_id,
                agent_name=agent_of(session_id, default="main"),
                blocks=[],
                attachments=[attachment.to_dict()],
            )
            sessions_dir = (
                self._sessions_dir_provider()
                if self._sessions_dir_provider is not None
                else None
            )
            if sessions_dir is not None:
                await self._transcript_store.append(
                    session_id, event, sessions_dir=sessions_dir
                )
            else:
                await self._transcript_store.append(session_id, event)
        except Exception:
            logger.exception(
                "Failed to persist outbound attachment %s for session %s; "
                "the file was sent but may not be downloadable.",
                attachment.id,
                session_id,
            )
