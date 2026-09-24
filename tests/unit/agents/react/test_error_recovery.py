"""Tests for the provider error recovery chain.

Covers:
- ``is_context_overflow_error`` marker detection
- ``EmergencyCompactionGovernance`` trimming logic (system + tail, user-start)
- ``attempt_recovery`` level escalation and max-retry boundary
- sameModel guard (FIRST attempt only): an overflow report from a stale
  model with a sufficient current budget retries unchanged instead of
  trimming; the second overflow always trims
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.agents.react.error_recovery import (
    EmergencyCompactionGovernance,
    ErrorRecoveryConfig,
    attempt_recovery,
    is_context_overflow_error,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage, MessageRole

_CTX: MagicMock = MagicMock(spec=AgentContext)
# Honest "no runtime bound" context: leaving a MagicMock here would leak a
# mock model_info into the sameModel budget resolution.
_CTX.runtime = None

# ---------------------------------------------------------------------------
# is_context_overflow_error
# ---------------------------------------------------------------------------


def test_is_context_overflow_413():
    assert is_context_overflow_error(RuntimeError("Request failed with 413"))


def test_is_context_overflow_context_length():
    assert is_context_overflow_error(
        ValueError("context_length_exceeded: too many tokens")
    )


def test_is_context_overflow_token_limit():
    assert is_context_overflow_error(Exception("token limit exceeded"))


def test_is_context_overflow_not_error():
    assert not is_context_overflow_error(RuntimeError("some other error"))


# ---------------------------------------------------------------------------
# EmergencyCompactionGovernance (still dict-based — governance ABC unchanged)
# ---------------------------------------------------------------------------


def _msg(role: str, content: str = "x") -> dict[str, object]:
    return {"role": role, "content": content}


def _make_messages(n: int, system: bool = True) -> list[dict[str, object]]:
    msgs: list[dict[str, object]] = []
    if system:
        msgs.append(_msg("system", "sys"))
    for i in range(n):
        msgs.append(_msg("user" if i % 2 == 0 else "assistant", str(i)))
    return msgs


@pytest.mark.asyncio
async def test_emergency_compaction_keeps_system_and_tail():
    messages = _make_messages(40)
    governance = EmergencyCompactionGovernance(keep_messages=10)
    result = await governance.apply(messages, _CTX)
    # system + up to 10 tail messages
    assert result[0]["role"] == "system"
    # The tail should be the last 10 of the non-system messages
    non_system_tail = messages[1:][-10:]
    assert result[1:] == non_system_tail


@pytest.mark.asyncio
async def test_emergency_compaction_level2():
    messages = _make_messages(40)
    governance = EmergencyCompactionGovernance(keep_messages=5)
    result = await governance.apply(messages, _CTX)
    assert result[0]["role"] == "system"
    # tail of 5 non-system messages, but leading non-user messages are dropped
    tail = messages[1:][-5:]
    while tail and tail[0]["role"] != "user":
        tail = tail[1:]
    assert result[1:] == tail


@pytest.mark.asyncio
async def test_emergency_compaction_ensures_user_start():
    # Build messages where the first of the tail is an assistant message.
    messages: list[dict[str, object]] = [_msg("system", "sys")]
    # Add 20 messages, starting with assistant so the tail starts with assistant.
    for i in range(20):
        if i < 10:
            messages.append(_msg("assistant", f"a{i}"))
        else:
            messages.append(_msg("user" if i % 2 == 0 else "assistant", f"m{i}"))
    governance = EmergencyCompactionGovernance(keep_messages=10)
    result = await governance.apply(messages, _CTX)
    assert result[0]["role"] == "system"
    # First non-system message must be "user"
    assert result[1]["role"] == "user"


@pytest.mark.asyncio
async def test_emergency_compaction_small_list():
    messages = _make_messages(3)
    governance = EmergencyCompactionGovernance(keep_messages=10)
    result = await governance.apply(messages, _CTX)
    # Fewer messages than keep_messages + 1 → unchanged (but a copy)
    assert result == messages
    assert result is not messages


# ---------------------------------------------------------------------------
# attempt_recovery (post-B6: accepts list[ChatMessage])
# ---------------------------------------------------------------------------


def _chat_msg(role: str, content: str = "x") -> ChatMessage:
    return ChatMessage(role=MessageRole(role), content=content)


def _make_chat_messages(n: int, system: bool = True) -> list[ChatMessage]:
    msgs: list[ChatMessage] = []
    if system:
        msgs.append(_chat_msg("system", "sys"))
    for i in range(n):
        msgs.append(_chat_msg("user" if i % 2 == 0 else "assistant", str(i)))
    return msgs


@pytest.mark.asyncio
async def test_attempt_recovery_level1():
    messages = _make_chat_messages(40)
    error = RuntimeError("413 Payload Too Large")
    recovery = await attempt_recovery(messages, error, 0, ErrorRecoveryConfig(), _CTX)
    assert recovery.should_retry is True
    assert recovery.trimmed_messages is not None
    assert "level 1" in recovery.reason
    # Should have trimmed to system + ≤10 messages
    assert len(recovery.trimmed_messages) < len(messages)


@pytest.mark.asyncio
async def test_attempt_recovery_level2():
    messages = _make_chat_messages(40)
    error = RuntimeError("context_length_exceeded")
    recovery = await attempt_recovery(messages, error, 1, ErrorRecoveryConfig(), _CTX)
    assert recovery.should_retry is True
    assert recovery.trimmed_messages is not None
    assert "level 2" in recovery.reason
    # Level 2 keeps fewer messages than level 1
    level1 = await attempt_recovery(messages, error, 0, ErrorRecoveryConfig(), _CTX)
    assert len(recovery.trimmed_messages) <= len(level1.trimmed_messages or [])


@pytest.mark.asyncio
async def test_attempt_recovery_max_retries():
    messages = _make_chat_messages(40)
    error = RuntimeError("413")
    # attempt_count == max_context_overflow_retries → no more retries
    recovery = await attempt_recovery(messages, error, 2, ErrorRecoveryConfig(), _CTX)
    assert recovery.should_retry is False
    assert "No recovery" in recovery.reason


@pytest.mark.asyncio
async def test_attempt_recovery_non_overflow():
    messages = _make_chat_messages(10)
    error = RuntimeError("connection reset by peer")
    recovery = await attempt_recovery(messages, error, 0, ErrorRecoveryConfig(), _CTX)
    assert recovery.should_retry is False
    assert "No recovery" in recovery.reason


# ---------------------------------------------------------------------------
# Additional marker coverage
# ---------------------------------------------------------------------------


def test_is_context_overflow_maximum_context():
    assert is_context_overflow_error(Exception("maximum context length exceeded"))


def test_is_context_overflow_too_long():
    assert is_context_overflow_error(ValueError("request too long"))


def test_is_context_overflow_payload_too_large():
    assert is_context_overflow_error(RuntimeError("413 Payload Too Large"))


# ---------------------------------------------------------------------------
# EmergencyCompactionGovernance edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_compaction_no_system_message():
    """No system message at index 0 → all messages are 'rest'."""
    messages = _make_messages(40, system=False)
    governance = EmergencyCompactionGovernance(keep_messages=10)
    result = await governance.apply(messages, _CTX)
    # No system message → result starts with a user message (or synthetic)
    assert result[0]["role"] in ("user", "system")
    assert len(result) <= 11  # no system + ≤10 tail


@pytest.mark.asyncio
async def test_emergency_compaction_all_assistant_tail():
    """Tail with no user message → synthetic user prepended."""
    messages: list[dict[str, object]] = [_msg("system", "sys")]
    # All assistant messages → tail has no user
    for i in range(20):
        messages.append(_msg("assistant", f"a{i}"))
    governance = EmergencyCompactionGovernance(keep_messages=10)
    result = await governance.apply(messages, _CTX)
    assert result[0]["role"] == "system"
    # Synthetic user message should be prepended
    assert result[1]["role"] == "user"
    assert "Continue" in str(result[1]["content"])


# ---------------------------------------------------------------------------
# sameModel guard (per-model compaction PRD §4.3.3)
# ---------------------------------------------------------------------------


def _context_with_model(limit: int | None) -> AgentContext:
    from modex_agent.core.capabilities import ModelInfo
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_agent.tools.manager import InMemoryToolManager

    model_info = ModelInfo(model_name="m", context_limit=limit)
    runtime = AgentRuntime(
        services=AgentRuntimeServices(model_info=model_info),
        state=None,  # type: ignore[arg-type]  # guard only reads model_info
    )
    return AgentContext(
        system_prompt="sys",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("s1.recovery"),
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_same_model_guard_retries_unchanged_within_budget():
    """Overflow error + current model's window comfortably large → no trim.

    The messages carry no cached token_count, so the CharTokenEstimator
    measures real (tiny) content — far below a 200k limit.
    """
    messages = _make_chat_messages(40)
    ctx = _context_with_model(limit=200_000)
    recovery = await attempt_recovery(messages, RuntimeError("413"), 0, ErrorRecoveryConfig(), ctx)
    assert recovery.should_retry is True
    assert recovery.trimmed_messages is None
    assert recovery.reason == (
        "overflow attributed to a different model; current budget sufficient"
    )


@pytest.mark.asyncio
async def test_same_model_guard_trims_when_current_budget_exceeded():
    """Small declared limit → the messages genuinely over the current model
    → the existing two-level trim applies."""
    messages = _make_chat_messages(40)
    ctx = _context_with_model(limit=1)
    recovery = await attempt_recovery(messages, RuntimeError("413"), 0, ErrorRecoveryConfig(), ctx)
    assert recovery.should_retry is True
    assert recovery.trimmed_messages is not None
    assert "level 1" in recovery.reason
    assert len(recovery.trimmed_messages) < len(messages)


@pytest.mark.asyncio
async def test_same_model_guard_inactive_without_model_info():
    """No runtime / no declared budget → the guard cannot judge → trim
    (regression of the pre-guard behavior)."""
    messages = _make_chat_messages(40)
    for ctx in (_CTX, _context_with_model(limit=None)):
        recovery = await attempt_recovery(
            messages, RuntimeError("413"), 0, ErrorRecoveryConfig(), ctx
        )
        assert recovery.should_retry is True
        assert recovery.trimmed_messages is not None


@pytest.mark.asyncio
async def test_same_model_guard_second_attempt_always_trims():
    """Second overflow trims even when the current budget still looks
    sufficient: the unchanged retry already happened once, so a low
    estimate must not keep shielding the messages — otherwise retry
    exhaustion turns into a hard failure."""
    messages = _make_chat_messages(40)
    ctx = _context_with_model(limit=200_000)
    recovery = await attempt_recovery(
        messages, RuntimeError("413"), 1, ErrorRecoveryConfig(), ctx
    )
    assert recovery.should_retry is True
    assert recovery.trimmed_messages is not None
    assert "level 2" in recovery.reason
    assert len(recovery.trimmed_messages) < len(messages)
