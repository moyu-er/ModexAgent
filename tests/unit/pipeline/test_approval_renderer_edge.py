"""ApprovalRenderer edge-case coverage for TurnSnapshot approval state."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.approval.response import parse_approval_action
from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.types import InputMessage
from modex_agent.pipeline.approval_renderer import ApprovalRenderer, format_approval_prompt
from modex_agent.runtime.enums import AgentKind, ApprovalSubjectType, SnapshotReason, TurnPhase
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
    TurnSnapshot,
)
from modex_agent.core.session_id import SessionInfo


def _pending_snapshot(session_id: str = "s1") -> TurnSnapshot:
    identity = TurnIdentity(agent_id="agent", session=SessionInfo.from_str(session_id), turn_id="t1")
    request = ApprovalRequestState(
        request_id="r1",
        approval_id="ap1",
        tool_call_id="c1",
        tool_name="write_file",
        arguments=ToolArguments(values={"path": "/dangerous"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=ApprovalTransaction(
            approval_id="ap1",
            turn_id=identity.turn_id,
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=["batch1"],
            requests=[request],
        ),
    )
    return ReActSnapshotPolicy().capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)


class TestDrain:
    def test_no_callback_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        renderer = ApprovalRenderer()
        renderer._approval_pending["s1"] = [InputMessage(content="x", session=SessionInfo.from_str("s1", default_agent_name="main"))]
        with caplog.at_level("WARNING"):
            asyncio.run(renderer.drain("s1"))
        assert "_on_drain is None" in caplog.text
        assert "s1" not in renderer._approval_pending

    def test_multiple_messages(self) -> None:
        renderer = ApprovalRenderer()
        drained: list[str] = []

        async def _mock(msg: InputMessage) -> None:
            drained.append(msg.content)

        renderer._on_drain = _mock
        renderer._approval_pending["s1"] = [
            InputMessage(content="a", session=SessionInfo.from_str("s1", default_agent_name="main")),
            InputMessage(content="b", session=SessionInfo.from_str("s1", default_agent_name="main")),
        ]
        asyncio.run(renderer.drain("s1"))

        async def _wait() -> list[str]:
            await asyncio.sleep(0)
            return drained

        assert set(asyncio.run(_wait())) == {"a", "b"}


class TestDetect:
    def test_no_pending_snapshot_returns_false(self) -> None:
        renderer = ApprovalRenderer()
        msg = InputMessage(content="hello", session=SessionInfo.from_str("s1", default_agent_name="main"))
        is_cmd, state = asyncio.run(
            renderer.detect(msg, "s1", {}, pending_snapshot=None)
        )
        assert is_cmd is False
        assert state is None

    def test_approval_command_detected(self) -> None:
        renderer = ApprovalRenderer()
        snapshot = _pending_snapshot()
        msg = InputMessage(content="/approve", session=SessionInfo.from_str("s1", default_agent_name="main"))
        is_cmd, state = asyncio.run(
            renderer.detect(
                msg,
                "s1",
                {},
                pending_snapshot=snapshot,
                approval_action=parse_approval_action(msg.content),
            )
        )
        assert is_cmd is True
        assert state is snapshot

    def test_unrelated_input_auto_denies(self) -> None:
        renderer = ApprovalRenderer()
        snapshot = _pending_snapshot()
        msg = InputMessage(content="random chat", session=SessionInfo.from_str("s1", default_agent_name="main"))
        is_cmd, state = asyncio.run(
            renderer.detect(msg, "s1", {}, pending_snapshot=snapshot)
        )
        assert is_cmd is True
        assert state is not None
        approval = ReActSnapshotPolicy.approval_from_snapshot(state)
        assert approval is not None
        assert approval.decisions["c1"] == ApprovalDecision.DENIED
        assert "unrelated input" in (approval.deny_reason or "")

    def test_source_agent_buffers_not_denies(self) -> None:
        renderer = ApprovalRenderer()
        snapshot = _pending_snapshot()
        msg = InputMessage(
            content="subagent update",
            session=SessionInfo.from_str("s1", default_agent_name="main"),
            metadata={"source_agent": "subagent-a"},
        )
        is_cmd, state = asyncio.run(
            renderer.detect(
                msg,
                "s1",
                {"source_agent": "subagent-a"},
                pending_snapshot=snapshot,
            )
        )
        assert is_cmd is True
        assert state is snapshot
        assert renderer._approval_pending["s1"][0] is msg
        approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
        assert approval is not None
        assert approval.decisions.get("c1") != ApprovalDecision.DENIED


class TestCleanup:
    def test_nonexistent_session_noop(self) -> None:
        renderer = ApprovalRenderer()
        renderer.cleanup_session("noexist")

    def test_removes_pending(self) -> None:
        renderer = ApprovalRenderer()
        renderer._approval_pending["s1"] = [InputMessage(content="x", session=SessionInfo.from_str("s1", default_agent_name="main"))]
        renderer.cleanup_session("s1")
        assert "s1" not in renderer._approval_pending


class TestFormat:
    def test_tool_name_and_args(self) -> None:
        view = ApprovalRequestView(
            tool_call_id="abc",
            tool_name="rm",
            tier=str(ApprovalTier.HARDLINE),
            arguments={"path": "/etc"},
        )
        result = format_approval_prompt(view)
        assert "HARDLINE" in result
        assert "rm" in result
        assert "abc" in result
        assert "path=/etc" in result

    def test_null_args(self) -> None:
        view = ApprovalRequestView(
            tool_call_id="x1",
            tool_name="echo",
            tier="normal",
            arguments={},
        )
        result = format_approval_prompt(view)
        assert "NORMAL" in result
        assert "echo" in result
