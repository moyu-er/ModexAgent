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

    # Total dump: the unset approval_id is carried as None here; the broker
    # boundary's exclude_none dump (BrokerInputPayload) is what hides it.
    assert payload == {"tool_call_id": None, "action": "deny", "approval_id": None}
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


def test_approval_decision_none_approval_id_uses_wire_exclude_none_contract() -> None:
    """The unset approval_id is hidden by the broker boundary's existing
    ``exclude_none=True`` payload dump (BrokerInputPayload) — never by a
    per-field ``exclude_if`` kwarg that the pydantic>=2.0 floor does not
    provide."""
    legacy = ApprovalDecisionInput(tool_call_id="t1", action=ApprovalAction.ALLOW)
    scoped = ApprovalDecisionInput(
        tool_call_id="t1", action=ApprovalAction.ALLOW, approval_id="ap1",
    )
    plain = legacy.model_dump(mode="json")
    assert plain["approval_id"] is None  # total dump carries None
    assert "approval_id" not in legacy.model_dump(mode="json", exclude_none=True)
    assert scoped.model_dump(mode="json", exclude_none=True)["approval_id"] == "ap1"
