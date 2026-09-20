"""Scripted ACP backend — deterministic smoke/demo backend for the bare entry.

Not an LLM. Implements the same ``AcpSessionBackend`` seam as the production
backend (DESIGN.md §8) so ``python -m modex_agent.acp`` is drivable out of the
box and the e2e turn test has a stable scripted turn source. Business wiring
passes a project-bound backend to ``entry.main`` instead.

Directives (first whitespace-separated token of the prompt):

- ``wait``    — emit one chunk, then block until ``handle.cancel``.
- ``approve`` — emit a Bash tool call, then await one permission decision.
- anything    — echo turn: reasoning + text + Read tool call/result + text.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

from .backend import AcpInteraction, AcpSessionBackend, AcpSessionHandle
from .types import (
    AcpBackendError,
    AcpBackendErrorCode,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPermissionOption,
    AcpPromptInput,
    PermissionPrompt,
)

__all__ = ["ScriptedAcpBackend"]

WAIT_DIRECTIVE = "wait"
APPROVE_DIRECTIVE = "approve"


class _ScriptedHandle(AcpSessionHandle):
    """One scripted session: echo / approve / wait directives."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._cancelled = asyncio.Event()

    @property
    def session_id(self) -> str:
        return self._session_id

    async def prompt(self, input: AcpPromptInput, interaction: AcpInteraction) -> AgentResult:
        self._cancelled.clear()
        tokens = input.text.strip().split(maxsplit=1)
        directive = tokens[0].lower() if tokens else ""
        if directive == WAIT_DIRECTIVE:
            return await self._run_wait(input.text, interaction)
        if directive == APPROVE_DIRECTIVE:
            return await self._run_approval(input.text, interaction, turn_id=uuid4().hex)
        return await self._run_echo(input.text, interaction)

    async def cancel(self) -> None:
        self._cancelled.set()

    async def read_history(self) -> list[ChatMessage]:
        """Scripted sessions have no persisted history (``supports_load`` is False)."""
        return []

    async def close(self) -> None:
        # release a possibly-waiting prompt so serve teardown can drain
        self._cancelled.set()

    async def _run_echo(self, text: str, interaction: AcpInteraction) -> AgentResult:
        await interaction.emit(TurnReasoningEvent(text=f"thinking about: {text}"))
        await interaction.emit(TurnTextEvent(text=f"echo: {text}"))
        await interaction.emit(
            TurnToolCallEvent(tool_name="Read", call_id="call-1", arguments={"path": "README.md"})
        )
        await interaction.emit(
            TurnToolResultEvent(tool_name="Read", call_id="call-1", output="scripted file contents")
        )
        await interaction.emit(TurnTextEvent(text="done"))
        return AgentResult(content="done", stop_reason=StopReason.COMPLETED)

    async def _run_approval(
        self, text: str, interaction: AcpInteraction, *, turn_id: str
    ) -> AgentResult:
        await interaction.emit(
            TurnToolCallEvent(tool_name="Bash", call_id="call-1", arguments={"command": text})
        )
        prompt = PermissionPrompt(
            approval_id=f"approval-{turn_id}",
            tool_call_id="call-1",
            tool_name="Bash",
            title=f"Bash ({text})",
        )
        request = asyncio.create_task(interaction.request_decision(prompt))
        cancelled = asyncio.create_task(self._cancelled.wait())
        try:
            # first signal wins: the editor's answer or the handle's
            # cancel/close — a cancelled prompt must never hang on a
            # front-end that will never answer
            await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # cancel AND drain both sub-tasks — a hard-cancelled parent
            # prompt task must not leak the permission round-trip
            request.cancel()
            cancelled.cancel()
            await asyncio.gather(request, cancelled, return_exceptions=True)
        if self._cancelled.is_set():
            # handle cancel/close won the race (an answer arriving in the
            # same window still loses to cancel): the turn ends cancelled —
            # a denial fact is never fabricated for a cancellation
            return AgentResult(content=None, stop_reason=StopReason.CANCELLED)
        choice = request.result()
        # a client dismiss (option=None) denies the request (DESIGN.md §6.3)
        # — it is a real denial outcome, not a session cancel
        outcome = "denied" if choice.option is not AcpPermissionOption.ALLOW_ONCE else "executed"
        await interaction.emit(TurnToolResultEvent(tool_name="Bash", call_id="call-1", output=outcome))
        await interaction.emit(TurnTextEvent(text=outcome))
        return AgentResult(content=outcome, stop_reason=StopReason.COMPLETED)

    async def _run_wait(self, text: str, interaction: AcpInteraction) -> AgentResult:
        await interaction.emit(TurnTextEvent(text="waiting"))
        await self._cancelled.wait()
        return AgentResult(content=None, stop_reason=StopReason.CANCELLED, partial_content="waiting")


class ScriptedAcpBackend(AcpSessionBackend):
    """Deterministic backend owning no persisted state; one handle per open."""

    def __init__(self) -> None:
        self._handles: list[_ScriptedHandle] = []

    @property
    def supports_load(self) -> bool:
        return False

    async def open(self, request: AcpOpenRequest) -> AcpSessionHandle:
        if request.kind is AcpOpenKind.LOAD:
            raise AcpBackendError(
                AcpBackendErrorCode.NOT_FOUND,
                f"scripted backend has no persisted sessions: {request.session_id}",
            )
        handle = _ScriptedHandle(uuid4().hex)
        self._handles.append(handle)
        return handle

    async def close(self) -> None:
        for handle in self._handles:
            await handle.close()
        self._handles.clear()
