"""Approval command parsing and pending snapshot detection."""

from __future__ import annotations

import asyncio
from pathlib import Path

from framework.agents.react.constants import ReActNode
from framework.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.approval.response import parse_input_command
from framework.approval.types import ApprovalAction
from framework.core.types import InputMessage
from framework.pipeline.approval_renderer import ApprovalRenderer
from framework.runtime.enums import AgentKind, ApprovalSubjectType, SnapshotReason, TurnPhase
from framework.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
)


def _snapshot_with_requests(*, count: int = 1):
    identity = TurnIdentity(agent_id="agent", session_id="s1", turn_id="t1")
    requests = [
        ApprovalRequestState(
            request_id=f"r{i}",
            approval_id="ap1",
            tool_call_id=f"c{i}",
            tool_name="write_file",
            arguments=ToolArguments(values={"path": f"/etc/{i}"}),
            tier=ApprovalTier.DANGEROUS,
            iteration=1,
        )
        for i in range(count)
    ]
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
            requests=requests,
        ),
    )
    return ReActSnapshotPolicy().capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)


def test_approve_is_command_only_when_pending_snapshot_exists() -> None:
    parsed = parse_input_command("/approve")
    assert parsed is not None
    assert parsed.approval_action == ApprovalAction.ALLOW

    renderer = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
    msg = InputMessage(content="/approve", session_id="s1")
    is_cmd, state = asyncio.run(
        renderer.detect(msg, "s1", {}, pending_snapshot=None, approval_action=parsed.approval_action)
    )
    assert is_cmd is False
    assert state is None


def test_approve_detected_against_pending_snapshot() -> None:
    parsed = parse_input_command("/approve")
    renderer = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
    snapshot = _snapshot_with_requests()
    is_cmd, state = asyncio.run(
        renderer.detect(
            InputMessage(content="/approve", session_id="s1"),
            "s1",
            {},
            pending_snapshot=snapshot,
            approval_action=parsed.approval_action if parsed else None,
        )
    )
    assert is_cmd is True
    assert state is snapshot


def test_non_slash_approval_aliases_are_not_commands() -> None:
    assert parse_input_command("approve") is None
    assert parse_input_command("yes") is None
    assert parse_input_command("ok") is None


def test_approval_command_ignores_extra_args() -> None:
    parsed = parse_input_command("/approve extra text")
    assert parsed is not None
    assert parsed.approval_action == ApprovalAction.ALLOW


def test_unrelated_input_denies_first_and_preempts_rest() -> None:
    renderer = ApprovalRenderer(approval_workspace=Path("/tmp/ar"))
    snapshot = _snapshot_with_requests(count=3)
    _, state = asyncio.run(
        renderer.detect(
            InputMessage(content="random chatter", session_id="s1"),
            "s1",
            {},
            pending_snapshot=snapshot,
        )
    )
    approval = ReActSnapshotPolicy.approval_from_snapshot(state)
    assert approval is not None
    assert approval.decisions == {
        "c0": ApprovalDecision.DENIED,
        "c1": ApprovalDecision.PREEMPTED,
        "c2": ApprovalDecision.PREEMPTED,
    }
    assert "unrelated input" in (approval.deny_reason or "")
