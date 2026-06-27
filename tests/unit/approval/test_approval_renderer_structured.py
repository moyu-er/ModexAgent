from __future__ import annotations

from modex_agent.approval.views import ApprovalRequestView
from modex_agent.pipeline.approval_renderer import approval_output_message, format_approval_prompt


def test_approval_output_message_carries_text_and_structured_view():
    view = ApprovalRequestView("call_1", "write_file", "dangerous", {"path": "a"}, "pending")
    msg = approval_output_message(view)
    # IM text fallback
    assert "write_file" in msg.content
    assert "Reply /approve or /deny" in msg.content
    assert "DANGEROUS" in msg.content  # tier upper-cased
    # webui structured tag + view
    assert msg.message_type == "approval_request"
    assert msg.metadata["approval"] == view.to_dict()


def test_format_approval_prompt_takes_view_and_keeps_text_shape():
    view = ApprovalRequestView("c1", "edit_file", "dangerous", {"path": "/x"}, "pending")
    text = format_approval_prompt(view)
    assert "Approval Required [DANGEROUS]" in text
    assert "Tool: edit_file" in text
    assert "ID: c1" in text
    assert "Reply /approve or /deny" in text
