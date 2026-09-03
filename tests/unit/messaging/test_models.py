from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput


def test_decision_input_carries_call_id_and_action() -> None:
    decision = ApprovalDecisionInput(tool_call_id="call_1", action=ApprovalAction.ALLOW)

    assert decision.tool_call_id == "call_1"
    assert decision.action is ApprovalAction.ALLOW


def test_decision_input_allows_null_call_id_roundtrip() -> None:
    decision = ApprovalDecisionInput(tool_call_id=None, action=ApprovalAction.DENY)

    payload = decision.model_dump(mode="json")

    assert payload == {"tool_call_id": None, "action": "deny"}
    assert ApprovalDecisionInput.model_validate(payload) == decision


def test_decision_input_is_frozen_and_forbids_extra_fields() -> None:
    decision = ApprovalDecisionInput(tool_call_id="call_1", action=ApprovalAction.ALLOW)

    with pytest.raises(ValidationError):
        decision.action = ApprovalAction.DENY  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ApprovalDecisionInput.model_validate(
            {
                "tool_call_id": "call_1",
                "action": "allow",
                "unexpected": True,
            }
        )
