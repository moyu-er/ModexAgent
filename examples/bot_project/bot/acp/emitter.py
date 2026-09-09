"""Editor output reuses the bot transcript recorder and routes real approval views."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bot.webui.emitter.bot_transcript import BotTranscriptEmitter
from bot.webui.events import SessionMeta
from bot.webui.transcript_store import TranscriptStore
from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.session_id import agent_of
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.messaging.models import OutputMessage, OutputMessageType

TurnEventListener = Callable[[TurnEvent], Awaitable[None]]
ApprovalRequestHandler = Callable[[str, ApprovalRequestView], Awaitable[None]]
"""Approval sink: ``(source session id, view)``.

The source session id is the session that OWNS the suspended turn (the
approval's SOURCE) — routing a child view to the root editor must never
erase which session the decision must resume.
"""
ChildSessionResolver = Callable[[str], Awaitable[str | None]]


class AcpApprovalRouteError(RuntimeError):
    """A suspended turn has no editor capable of deciding its request."""


class AcpEmitterHub:
    def __init__(self, resolver: ChildSessionResolver | None = None) -> None:
        self._listeners: dict[str, TurnEventListener] = {}
        self._approvals: dict[str, ApprovalRequestHandler] = {}
        self._resolver = resolver

    def register(self, session_id: str, on_event: TurnEventListener, on_approval: ApprovalRequestHandler | None = None) -> None:
        if session_id in self._listeners:
            raise RuntimeError(f"Session {session_id} already has an output listener")
        self._listeners[session_id] = on_event
        if on_approval is not None:
            self._approvals[session_id] = on_approval

    def unregister(self, session_id: str) -> None:
        self._listeners.pop(session_id, None)
        self._approvals.pop(session_id, None)

    async def _owner_sid(self, session_id: str) -> str | None:
        """The open editor session owning ``session_id``'s output.

        One routing rule for every sink: a directly-registered session is
        its own owner; anything else rides the parent-chain resolver to its
        open root. ``None`` means no owner (unresolvable or unregistered).
        """
        if session_id in self._listeners:
            return session_id
        if self._resolver is None:
            return None
        return await self._resolver(session_id)

    async def emit(self, session_id: str, event: TurnEvent) -> None:
        owner = await self._owner_sid(session_id)
        listener = self._listeners.get(owner) if owner is not None else None
        if listener is None:
            return
        if owner == session_id:
            await listener(event)
            return
        projected: TurnEvent
        match event:
            case TurnToolCallEvent(call_id=call_id) | TurnToolResultEvent(call_id=call_id):
                projected = event.model_copy(
                    update={"call_id": f"{session_id}:{call_id}"}
                )
            case TurnTextEvent(text=text) | TurnReasoningEvent(text=text):
                projected = event.model_copy(
                    update={"text": f"[{agent_of(session_id)}] {text}"}
                )
        await listener(projected)

    async def approval(self, session_id: str, view: ApprovalRequestView) -> None:
        """Route one approval view to its owning editor, keeping the SOURCE.

        Same owner rule as ``emit`` (one lookup helper, no second routing
        tree). The handler always receives the approval's SOURCE session id
        so the decision resumes the session that owns the suspended turn —
        never the root by accident. No owner means an unroutable approval:
        raise so the owner cancels the scope instead of a silent drop.
        """
        owner = await self._owner_sid(session_id)
        handler = self._approvals.get(owner) if owner is not None else None
        if handler is None:
            raise AcpApprovalRouteError(
                f"No editor approval handler for session {session_id} (owner={owner!r})"
            )
        await handler(session_id, view)


class AcpOutputAdapter(OutputAdapter):
    def __init__(self, hub: AcpEmitterHub) -> None:
        self._hub = hub

    @property
    def name(self) -> str:
        return "acp"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NATIVE

    async def send(self, message: OutputMessage, session_id: str) -> None:
        if message.message_type == OutputMessageType.APPROVAL_REQUEST:
            raw = message.metadata.get("approval")
            if not isinstance(raw, dict):
                raise AcpApprovalRouteError(f"Invalid approval payload for session {session_id}")
            await self._hub.approval(session_id, ApprovalRequestView(**raw))
        elif message.content:
            await self._hub.emit(session_id, TurnTextEvent(text=message.content))

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        if delta:
            await self._hub.emit(session_id, TurnTextEvent(text=delta))

    async def flush_deltas(self, session_id: str) -> None:
        pass


class AcpTurnEmitter(BotTranscriptEmitter):
    """ACP editor projection — records the canonical transcript via the
    shared base lifecycle, projects every fact as a full-fidelity
    ``TurnEvent`` through the hub (full args / full result, no WS-style
    display truncation).
    """

    def __init__(
        self, hub: AcpEmitterHub, session_id: str, *, pool: str | None = None,
        transcript_store: TranscriptStore | None = None,
        sessions_dir_provider: Callable[[], Path | None] | None = None,
        session_meta_resolver: Callable[[], SessionMeta] | None = None,
    ) -> None:
        super().__init__(
            AcpOutputAdapter(hub), session_id, pool=pool,
            transcript_store=transcript_store,
            sessions_dir_provider=sessions_dir_provider,
            session_meta_resolver=session_meta_resolver,
        )
        self._hub = hub

    # ------------------------------------------------------------------
    # ACP projection (full-fidelity TurnEvents to the owning editor)
    # ------------------------------------------------------------------

    async def _project_text_delta(self, text: str, part_id: str | None) -> None:
        await self._hub.emit(self._session_id, TurnTextEvent(text=text, part_id=part_id))

    async def _project_reasoning_delta(self, text: str, part_id: str | None) -> None:
        await self._hub.emit(
            self._session_id, TurnReasoningEvent(text=text, part_id=part_id)
        )

    async def _project_tool_start(
        self,
        tool_name: str,
        call_id: str,
        full_args: dict[str, Any],
    ) -> None:
        await self._hub.emit(
            self._session_id,
            TurnToolCallEvent(
                tool_name=tool_name,
                call_id=call_id,
                arguments=full_args,
            ),
        )

    async def _project_tool_end(
        self,
        tool_name: str,
        call_id: str | None,
        full_result: str,
        seq: int | None,
    ) -> None:
        if call_id is None:
            return
        await self._hub.emit(
            self._session_id,
            TurnToolResultEvent(
                tool_name=tool_name,
                call_id=call_id,
                output=full_result,
            ),
        )

    async def _project_turn_end(self, latency_ms: int) -> None:
        """ACP editors derive turn boundaries from session update state, not
        from an emitter event — no turn_end projection."""

