"""Tests for TurnOutcomeNotifyHook — user notice on the two silent abnormal ends.

Scope is intentionally narrow: a real exception, or max-iterations. Pause/cancel
and approval suspension already notify the user through their own paths, so the
hook must NOT fire for them.
"""

from __future__ import annotations

import pytest

from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.notification import TurnOutcomeNotifyHook
from modex_agent.memory.history import ListMessageHistory


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNoticeService:
    """Records send_notice calls; stands in for AgentNotificationService."""

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    async def send_notice(self, session_id: str, text: str) -> None:
        self.notices.append((session_id, text))


def _make_context(comm_kind: AgentCommKind | None = AgentCommKind.NORMAL) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo.from_str("s.main"),
        comm_kind=comm_kind,
    )


# ---------------------------------------------------------------------------
# Tests — in scope (notify)
# ---------------------------------------------------------------------------


class TestTurnOutcomeNotifyHook:
    @pytest.mark.asyncio
    async def test_error_with_message_sends_notice(self) -> None:
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(
            _make_context(), AgentResult(stop_reason=StopReason.ERROR, error="boom")
        )
        assert len(svc.notices) == 1
        assert "error" in svc.notices[0][1].lower()

    @pytest.mark.asyncio
    async def test_max_iterations_sends_notice(self) -> None:
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(_make_context(), AgentResult(stop_reason=StopReason.MAX_ITERATIONS))
        assert len(svc.notices) == 1
        assert "maximum" in svc.notices[0][1].lower()


# ---------------------------------------------------------------------------
# Tests — out of scope / already-notified (must NOT notify)
# ---------------------------------------------------------------------------


class TestTurnOutcomeNotifyHookExclusions:
    @pytest.mark.asyncio
    async def test_graphinterrupt_false_positive_skipped(self) -> None:
        """GraphInterrupt leaves the initial result (ERROR, error=None) — must not
        fire, or approval suspension would get a duplicate 'error' notice."""
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(
            _make_context(),
            AgentResult(stop_reason=StopReason.ERROR, error=None),  # initial result
        )
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_cancelled_skipped(self) -> None:
        """Pause/cancel already acks the user via the control channel."""
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(_make_context(), AgentResult(stop_reason=StopReason.CANCELLED))
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_turn_cancelled_skipped(self) -> None:
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(_make_context(), AgentResult(stop_reason=StopReason.TURN_CANCELLED))
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_timeout_skipped(self) -> None:
        """Timeout is out of scope for this hook."""
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(_make_context(), AgentResult(stop_reason=StopReason.TIMEOUT))
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_completed_skipped(self) -> None:
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(
            _make_context(), AgentResult(stop_reason=StopReason.COMPLETED, content="done")
        )
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_other_reason_with_error_skipped(self) -> None:
        """Only ERROR+error and MAX_ITERATIONS are in scope; other reasons are not."""
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(
            _make_context(),
            AgentResult(stop_reason=StopReason.MISSED_COMMUNICATION, error="something"),
        )
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_subagent_skipped(self) -> None:
        """Subagent outcomes are handled by other hooks — this one is NORMAL-only."""
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(
            _make_context(comm_kind=AgentCommKind.SUBAGENT),
            AgentResult(stop_reason=StopReason.ERROR, error="boom"),
        )
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_none_result_skipped(self) -> None:
        svc = _FakeNoticeService()
        hook = TurnOutcomeNotifyHook(notification_service=svc)  # type: ignore[arg-type]
        await hook.finally_graph(_make_context(), None)
        assert svc.notices == []

    @pytest.mark.asyncio
    async def test_no_service_skipped(self) -> None:
        hook = TurnOutcomeNotifyHook(notification_service=None)
        # Must not raise.
        await hook.finally_graph(
            _make_context(), AgentResult(stop_reason=StopReason.ERROR, error="x")
        )

    @pytest.mark.asyncio
    async def test_name(self) -> None:
        assert TurnOutcomeNotifyHook().name == "turn_outcome_notify"
