"""Unit tests for ``ScriptedAcpBackend`` — the scripted ``AcpSessionBackend``.

Same seam as the production backend (DESIGN.md §8): open/prompt/cancel/
read_history/close, no LLM. Directives: ``wait`` (block until cancel),
``approve`` (one-request approval batch), anything else (echo turn).
"""

import asyncio
from pathlib import Path

import pytest

from modex_agent.acp.backend import AcpInteraction, AcpSessionHandle
from modex_agent.acp.scripted import ScriptedAcpBackend
from modex_agent.acp.types import (
    AcpBackendError,
    AcpBackendErrorCode,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPermissionOption,
    AcpPromptInput,
    PermissionChoice,
    PermissionPrompt,
)
from modex_agent.core.emitter import StopReason
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolResultEvent,
)


class RecordingInteraction(AcpInteraction):
    """Records emitted events; replays queued permission choices."""

    def __init__(self, choices: list[PermissionChoice]) -> None:
        self.events: list[TurnEvent] = []
        self.prompts: list[PermissionPrompt] = []
        self._choices = list(choices)

    async def emit(self, event: TurnEvent) -> None:
        self.events.append(event)

    async def request_decision(self, prompt: PermissionPrompt) -> PermissionChoice:
        self.prompts.append(prompt)
        return self._choices.pop(0)


class HangingApprovalInteraction(AcpInteraction):
    """Simulates an editor that holds the permission request until released.

    ``settled`` fires when the request_decision coroutine reaches a terminal
    state (returned or was cancelled) — the regression observable for the
    prompt task's finally-block drain.
    """

    def __init__(self) -> None:
        self.events: list[TurnEvent] = []
        self.prompts: list[PermissionPrompt] = []
        self.requested = asyncio.Event()
        self.settled = asyncio.Event()
        self._release = asyncio.Event()

    def release(self) -> None:
        """Deliver the editor's answer (allow)."""
        self._release.set()

    async def emit(self, event: TurnEvent) -> None:
        self.events.append(event)

    async def request_decision(self, prompt: PermissionPrompt) -> PermissionChoice:
        self.prompts.append(prompt)
        self.requested.set()
        try:
            await self._release.wait()
            return PermissionChoice(option=AcpPermissionOption.ALLOW_ONCE)
        finally:
            self.settled.set()


def _backend() -> ScriptedAcpBackend:
    return ScriptedAcpBackend()


async def test_open_new_returns_fresh_handle_with_generated_session_id() -> None:
    backend = _backend()
    first = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/proj")))
    second = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/proj")))
    assert first.session_id and second.session_id
    assert first.session_id != second.session_id


async def test_supports_load_is_false_and_load_is_rejected() -> None:
    backend = _backend()
    assert backend.supports_load is False  # scripted history is not a real source
    with pytest.raises(AcpBackendError) as exc_info:
        await backend.open(AcpOpenRequest(kind=AcpOpenKind.LOAD, cwd=Path("D:/p"), session_id="s1"))
    assert exc_info.value.code is AcpBackendErrorCode.NOT_FOUND


async def test_close_closes_all_opened_handles() -> None:
    backend = _backend()
    handles = [
        await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p"))) for _ in range(2)
    ]
    await backend.close()
    assert all(isinstance(h, AcpSessionHandle) for h in handles)
    await backend.close()  # idempotent


async def test_echo_prompt_streams_framework_events_and_completes() -> None:
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = RecordingInteraction(choices=[])
    result = await handle.prompt(AcpPromptInput(text="hello world"), interaction)
    assert result.stop_reason is StopReason.COMPLETED
    assert any(isinstance(e, TurnTextEvent) and e.text == "echo: hello world" for e in interaction.events)
    assert any(isinstance(e, TurnToolResultEvent) for e in interaction.events)


async def test_approve_prompt_round_trips_permission_with_once_options() -> None:
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = RecordingInteraction(
        choices=[PermissionChoice(option=AcpPermissionOption.ALLOW_ONCE)]
    )
    result = await handle.prompt(AcpPromptInput(text="approve rm -rf /tmp/x"), interaction)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.content == "executed"
    (prompt,) = interaction.prompts
    assert prompt.tool_call_id == "call-1"
    assert prompt.options == (AcpPermissionOption.ALLOW_ONCE, AcpPermissionOption.REJECT_ONCE)


async def test_deny_prompt_reports_denied_outcome() -> None:
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = RecordingInteraction(
        choices=[PermissionChoice(option=AcpPermissionOption.REJECT_ONCE)]
    )
    result = await handle.prompt(AcpPromptInput(text="approve rm"), interaction)
    assert result.content == "denied"
    decision = interaction.prompts  # one decision requested
    assert len(decision) == 1


async def test_client_dismissed_permission_is_a_denial_not_a_cancel() -> None:
    """DESIGN.md §6.3: the user dismissing the permission card is a denial of
    the request — the turn completes with a real denied outcome."""
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = RecordingInteraction(choices=[PermissionChoice(option=None)])
    result = await handle.prompt(AcpPromptInput(text="approve rm"), interaction)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.content == "denied"
    assert any(
        isinstance(e, TurnToolResultEvent) and e.output == "denied" for e in interaction.events
    )
    assert interaction.prompts[0].tool_call_id == "call-1"


async def test_wait_prompt_cancels_to_cancelled_result() -> None:
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = RecordingInteraction(choices=[])
    task = asyncio.create_task(handle.prompt(AcpPromptInput(text="wait"), interaction))
    await asyncio.sleep(0)
    await handle.cancel()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason is StopReason.CANCELLED
    assert result.partial_content == "waiting"


async def test_cancel_during_suspended_approval_ends_the_prompt() -> None:
    """session/cancel during a pending permission request must end the turn:
    the prompt resolves as cancelled, no execution result is fabricated, and
    the task never hangs on the dead editor."""
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = HangingApprovalInteraction()
    task = asyncio.create_task(handle.prompt(AcpPromptInput(text="approve rm"), interaction))
    await asyncio.wait_for(interaction.requested.wait(), timeout=5)
    await handle.cancel()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason is StopReason.CANCELLED
    # cancellation is not a denial — no fake denied execution may be emitted
    assert not any(isinstance(e, TurnToolResultEvent) for e in interaction.events)
    assert not any(isinstance(e, TurnTextEvent) and e.text in ("executed", "denied") for e in interaction.events)


async def test_close_during_suspended_approval_ends_the_prompt() -> None:
    """EOF/close during a pending permission request must end the turn the
    same way cancel does — the drain never waits on a dead editor."""
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = HangingApprovalInteraction()
    task = asyncio.create_task(handle.prompt(AcpPromptInput(text="approve rm"), interaction))
    await asyncio.wait_for(interaction.requested.wait(), timeout=5)
    await handle.close()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason is StopReason.CANCELLED
    assert not any(isinstance(e, TurnToolResultEvent) for e in interaction.events)
    assert not any(isinstance(e, TurnTextEvent) and e.text in ("executed", "denied") for e in interaction.events)


async def test_hard_cancelled_prompt_task_drains_the_permission_subtask() -> None:
    """A hard task-cancel of the whole prompt coroutine must not leak the
    in-flight permission round-trip: the finally block cancels AND awaits
    both sub-tasks before the CancelledError propagates."""
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = HangingApprovalInteraction()
    task = asyncio.create_task(handle.prompt(AcpPromptInput(text="approve rm"), interaction))
    await asyncio.wait_for(interaction.requested.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    # the permission round-trip reached its terminal state (cancelled and
    # awaited in the finally) instead of lingering as a background task
    await asyncio.wait_for(interaction.settled.wait(), timeout=5)


async def test_cancel_wins_when_answer_and_cancel_race() -> None:
    """When the editor answer and the handle cancel arrive in the same
    window, cancel wins — the turn ends cancelled with no fabricated result."""
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    interaction = HangingApprovalInteraction()
    task = asyncio.create_task(handle.prompt(AcpPromptInput(text="approve rm"), interaction))
    await asyncio.wait_for(interaction.requested.wait(), timeout=5)
    interaction.release()  # the answer arrives ...
    await handle.cancel()  # ... and cancel arrives with it
    result = await asyncio.wait_for(task, timeout=5)
    assert result.stop_reason is StopReason.CANCELLED
    assert not any(isinstance(e, TurnToolResultEvent) for e in interaction.events)
    assert not any(isinstance(e, TurnTextEvent) and e.text in ("executed", "denied") for e in interaction.events)


async def test_read_history_is_empty() -> None:
    backend = _backend()
    handle = await backend.open(AcpOpenRequest(kind=AcpOpenKind.NEW, cwd=Path("D:/p")))
    assert await handle.read_history() == []
