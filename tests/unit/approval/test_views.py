from __future__ import annotations
from modex_agent.approval.views import (
    ApprovalDecisionInput, ApprovalRequestView, view_from_request,
)
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.types import ApprovalAction
from modex_agent.runtime.models import ApprovalRequestState, ToolArguments


def test_view_from_request_serializes_all_fields():
    req = ApprovalRequestState(
        request_id="r1",
        approval_id="a1",
        tool_call_id="call_1",
        tool_name="write_file",
        tier=ApprovalTier.DANGEROUS,
        arguments=ToolArguments(values={"path": "/tmp/x", "content": "hi"}),
        iteration=0,
    )
    view = view_from_request(req)
    assert view == ApprovalRequestView(
        tool_call_id="call_1", tool_name="write_file",
        tier=str(ApprovalTier.DANGEROUS),
        arguments={"path": "/tmp/x", "content": "hi"},
        status="pending",
    )


def test_view_to_dict_roundtrip():
    view = ApprovalRequestView(
        tool_call_id="c", tool_name="edit_file",
        tier="dangerous", arguments={"path": "a"}, status="pending",
    )
    d = view.to_dict()
    assert d["tool_call_id"] == "c" and d["arguments"] == {"path": "a"}


def test_decision_input_carries_call_id_and_action():
    di = ApprovalDecisionInput(tool_call_id="call_1", action=ApprovalAction.ALLOW)
    assert di.tool_call_id == "call_1"
    assert di.action == ApprovalAction.ALLOW


def test_decision_input_allows_null_call_id_roundtrip():
    di = ApprovalDecisionInput(tool_call_id=None, action=ApprovalAction.DENY)
    assert di.tool_call_id is None
    d = di.to_dict()
    assert d == {"tool_call_id": None, "action": "deny"}
    assert ApprovalDecisionInput.from_dict(d) == di
