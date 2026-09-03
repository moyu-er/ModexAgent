"""Deduplicator must bypass structured approval decisions but still drop plain dups.

The dedup guard added in Task 2.2 (``input_msg.approval_decision is None``) has
two contracts:
  1. An InputMessage carrying an ``approval_decision`` (webui decision) is NEVER
     dropped by the deduplicator, even though its content is "" (which would
     otherwise hash-collide with prior empty-content messages).
  2. A plain duplicate message (no decision) is STILL dropped — the bypass is
     specific to decisions, not a blanket skip.

These assert on whether ``_turn_runner.process_locked`` was reached (spy),
proving the guard is surgical.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.utils.deduplicator import MessageDeduplicator
from modex_agent.tools.manager import InMemoryToolManager

from tests.unit.pipeline._helpers import _make_react_pipeline


class _InputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def receive(self):
        if False:
            yield None


class _OutputAdapter:
    async def send(self, message, session_id) -> None: ...
    async def send_delta(self, delta: str, session_id: str) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...

    @property
    def supports_streaming(self) -> bool:
        return False


class _Agent:
    name = "agent"

    async def run(self, context, emitter):
        return AgentResult(content="done")


class _AlwaysDuplicateDeduplicator(MessageDeduplicator):
    """Stub that reports EVERY message_id as a duplicate."""

    def is_duplicate(self, message_id: str) -> bool:  # noqa: D401
        return True


def _pipeline(*, deduplicator: MessageDeduplicator | None):
    """Minimal AgentPipeline with the given deduplicator (no command_processor)."""
    return _make_react_pipeline(
        agent=_Agent(),
        context_manager=InMemoryContextManager(),
        tool_manager=InMemoryToolManager(),
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        sanitizer=None,
        deduplicator=deduplicator,
    )


def _spy_process_locked(pipeline) -> list[dict[str, Any]]:
    """Replace ``process_locked`` with a recording coroutine; return the call log."""
    calls: list[dict[str, Any]] = []
    original = pipeline._turn_runner.process_locked

    async def _record(input_msg, session_id, route_result=None, *, session):  # noqa: ANN001
        calls.append({
            "input_msg": input_msg,
            "session_id": session_id,
            "session": session,
        })
        return None

    pipeline._turn_runner.process_locked = _record  # type: ignore[method-assign]
    # Keep a reference so the linter doesn't warn; original is unused beyond type.
    del original
    return calls


@pytest.mark.asyncio
async def test_approval_decision_bypasses_deduplicator() -> None:
    """A webui approval_decision reaches process_locked despite the deduplicator."""
    pipeline = _pipeline(deduplicator=_AlwaysDuplicateDeduplicator())
    calls = _spy_process_locked(pipeline)

    decision_msg = InputMessage(
        content="",
        session=SessionInfo.from_str("s:main"),
        approval_decision=ApprovalDecisionInput("call_1", ApprovalAction.DENY),
    )
    await pipeline._process_message(decision_msg)

    assert len(calls) == 1, "approval_decision must bypass dedup and reach the turn runner"
    assert calls[0]["input_msg"].approval_decision is not None
    assert calls[0]["input_msg"].approval_decision.tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_plain_duplicate_is_still_dropped() -> None:
    """A plain duplicate (no decision) is still dropped — guard is specific."""
    pipeline = _pipeline(deduplicator=_AlwaysDuplicateDeduplicator())
    calls = _spy_process_locked(pipeline)

    plain_msg = InputMessage(content="hello", session=SessionInfo.from_str("s:main"))
    result = await pipeline._process_message(plain_msg)

    assert result is None
    assert calls == [], "plain duplicate must NOT reach the turn runner"
