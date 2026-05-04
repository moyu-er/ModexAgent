"""ApprovalRenderer edge-case coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.types import InputMessage
from framework.pipeline.approval_renderer import ApprovalRenderer, format_approval_prompt


def _pending_state(session_id: str = "s1") -> ApprovalState:
    return ApprovalState(
        session_id=session_id,
        requests=[
            ApprovalRequest(
                tool_name="write_file", tool_call_id="c1",
                arguments={"path": "/dangerous"}, tier="dangerous", iteration=1,
            ),
        ],
    )


def _mock_prebuilt_with_strategy():
    strategy = MagicMock()
    strategy.load_approval_state = AsyncMock()
    strategy.save_approval_state = AsyncMock()
    strategy.delete_approval_state = AsyncMock()
    strategy.delete_resume_state = AsyncMock()
    strategy.load_resume_state = AsyncMock()
    prebuilt = MagicMock()
    prebuilt.approval = MagicMock()
    prebuilt.approval.suspend_strategy = strategy
    return prebuilt, strategy


class TestDrain:
    """drain() edge cases."""

    def test_no_callback_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        r._approval_pending["s1"] = [InputMessage(content="x", session_id="s1")]
        with caplog.at_level("WARNING"):
            asyncio.run(r._drain("s1"))
        assert "_on_drain is None" in caplog.text
        assert "s1" not in r._approval_pending

    def test_empty_noop(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        asyncio.run(r._drain("s1"))

    def test_multiple_messages(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        drained: list[str] = []

        async def _mock(msg: InputMessage) -> None:
            drained.append(msg.content)

        r._on_drain = _mock
        r._approval_pending["s1"] = [
            InputMessage(content="a", session_id="s1"),
            InputMessage(content="b", session_id="s1"),
        ]
        asyncio.run(r._drain("s1"))
        assert "s1" not in r._approval_pending
        # Yield so create_task callbacks run
        async def _wait() -> list[str]:
            await asyncio.sleep(0)
            return drained
        result = asyncio.run(_wait())
        assert set(result) == {"a", "b"}


class TestDetect:
    """detect() edge cases."""

    def test_no_prebuilt_runtime_returns_false(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        msg = InputMessage(content="hello", session_id="s1")
        is_cmd, state = asyncio.run(r.detect(msg, "s1", {}))
        assert is_cmd is False
        assert state is None

    def test_approval_command_detected(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        prebuilt, strategy = _mock_prebuilt_with_strategy()
        pending = _pending_state()
        strategy.load_approval_state.return_value = pending
        msg = InputMessage(content="/approve", session_id="s1")
        is_cmd, state = asyncio.run(r.detect(msg, "s1", {}, prebuilt_runtime=prebuilt))
        assert is_cmd is True
        assert state is pending

    def test_deny_command_detected(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        prebuilt, strategy = _mock_prebuilt_with_strategy()
        pending = _pending_state()
        strategy.load_approval_state.return_value = pending
        msg = InputMessage(content="/deny", session_id="s1")
        is_cmd, state = asyncio.run(r.detect(msg, "s1", {}, prebuilt_runtime=prebuilt))
        assert is_cmd is True

    def test_unrelated_input_auto_denies(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        prebuilt, strategy = _mock_prebuilt_with_strategy()
        pending = _pending_state()
        strategy.load_approval_state.return_value = pending
        msg = InputMessage(content="random chat", session_id="s1")
        is_cmd, state = asyncio.run(r.detect(msg, "s1", {}, prebuilt_runtime=prebuilt))
        assert is_cmd is False
        assert state is not None
        assert state.decisions.get("c1") == ApprovalDecision.DENIED
        assert "unrelated input" in (state.deny_reason or "")
        strategy.save_approval_state.assert_awaited_once_with(pending)

    def test_source_agent_buffers_not_denies(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        prebuilt, strategy = _mock_prebuilt_with_strategy()
        pending = _pending_state()
        strategy.load_approval_state.return_value = pending
        msg = InputMessage(content="peer update", session_id="s1",
                          metadata={"source_agent": "peer-a"})
        is_cmd, state = asyncio.run(r.detect(msg, "s1", {"source_agent": "peer-a"}, prebuilt_runtime=prebuilt))
        assert is_cmd is False  # not a command, but buffered
        assert "s1" in r._approval_pending
        assert r._approval_pending["s1"][0] is msg
        # should NOT auto-deny
        assert pending.decisions.get("c1") != ApprovalDecision.DENIED


class TestCleanup:
    def test_nonexistent_session_noop(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        r.cleanup_session("noexist")  # no raise

    def test_removes_all(self) -> None:
        r = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
        r._approval_stores["s1"] = MagicMock()
        r._resume_stores["s1"] = MagicMock()
        r._approval_pending["s1"] = [InputMessage(content="x", session_id="s1")]
        r.cleanup_session("s1")
        assert "s1" not in r._approval_stores
        assert "s1" not in r._resume_stores
        assert "s1" not in r._approval_pending


class TestFormat:
    def test_tool_name_and_args(self) -> None:
        req = MagicMock(tool_name="rm", tool_call_id="abc",
                       arguments={"path": "/etc"}, tier="hardline")
        result = format_approval_prompt(req)
        assert "HARDLINE" in result
        assert "rm" in result
        assert "abc" in result

    def test_null_args(self) -> None:
        req = MagicMock(tool_name="echo", tool_call_id="x1",
                       arguments=None, tier="normal")
        result = format_approval_prompt(req)
        assert "NORMAL" in result
        assert "echo" in result
