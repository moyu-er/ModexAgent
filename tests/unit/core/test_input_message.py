from __future__ import annotations
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.approval.types import ApprovalAction


def test_input_message_defaults_no_approval_decision():
    msg = InputMessage(content="hi", session=SessionInfo.from_str("s.main"))
    assert msg.approval_decision is None


def test_input_message_carries_approval_decision():
    di = ApprovalDecisionInput(tool_call_id="c1", action=ApprovalAction.ALLOW)
    msg = InputMessage(content="", session=SessionInfo.from_str("s.main"), approval_decision=di)
    assert msg.approval_decision is di
