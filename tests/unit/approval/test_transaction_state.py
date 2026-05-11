"""Tests for ApprovalTransaction — denormalized, preemption, batch atomicity."""
from __future__ import annotations

from framework.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from framework.runtime.enums import ApprovalSubjectType
from framework.runtime.models import ApprovalRequestState, ApprovalTransaction, ToolArguments


def test_denial_preempts_unresolved_requests() -> None:
    tx = ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[
            ApprovalRequestState(
                request_id="r1",
                approval_id="ap-1",
                tool_call_id="call-1",
                tool_name="write_file",
                arguments=ToolArguments(values={"path": "a.txt"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
            ApprovalRequestState(
                request_id="r2",
                approval_id="ap-1",
                tool_call_id="call-2",
                tool_name="delete_file",
                arguments=ToolArguments(values={"path": "b.txt"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
        ],
    )

    tx.apply_decision("call-1", ApprovalDecision.DENIED, reason="not allowed")

    assert tx.status is ApprovalStatus.DENIED
    assert tx.decisions["call-1"] == ApprovalDecision.DENIED
    assert tx.decisions["call-2"] == ApprovalDecision.PREEMPTED
    assert tx.deny_reason == "not allowed"


def test_allow_single_request_marks_partial() -> None:
    tx = ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[
            ApprovalRequestState(
                request_id="r1", approval_id="ap-1", tool_call_id="call-1",
                tool_name="read_file", arguments=ToolArguments(values={"path": "a.txt"}),
                tier=ApprovalTier.DANGEROUS, iteration=1,
            ),
            ApprovalRequestState(
                request_id="r2", approval_id="ap-1", tool_call_id="call-2",
                tool_name="read_file", arguments=ToolArguments(values={"path": "b.txt"}),
                tier=ApprovalTier.DANGEROUS, iteration=1,
            ),
        ],
    )

    tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
    assert tx.status is ApprovalStatus.PARTIAL
    assert tx.decisions["call-1"] == ApprovalDecision.ALLOWED


def test_all_allowed_marks_approved() -> None:
    tx = ApprovalTransaction(
        approval_id="ap-1", turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[
            ApprovalRequestState(
                request_id="r1", approval_id="ap-1", tool_call_id="call-1",
                tool_name="read_file", arguments=ToolArguments(values={"path": "a.txt"}),
                tier=ApprovalTier.DANGEROUS, iteration=1,
            ),
            ApprovalRequestState(
                request_id="r2", approval_id="ap-1", tool_call_id="call-2",
                tool_name="read_file", arguments=ToolArguments(values={"path": "b.txt"}),
                tier=ApprovalTier.DANGEROUS, iteration=1,
            ),
        ],
    )

    tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
    tx.apply_decision("call-2", ApprovalDecision.ALLOWED)
    assert tx.status is ApprovalStatus.APPROVED
