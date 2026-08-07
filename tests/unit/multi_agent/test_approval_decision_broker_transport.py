"""Approval decisions must survive the broker transport.

The webui approval decision rides on ``InputMessage.approval_decision``. In
production the InputMessage crosses the message broker (input adapter -> bridge
-> broker -> pool ``_dispatch_agent_message`` -> ``process_message``). If the
broker hop drops ``approval_decision``, the decision arrives as an empty user
turn: it pollutes history with empty ``role=user`` messages and, worse, the
resumed turn re-enters at the LLM node with a dangling assistant ``tool_calls``
(never executed) -> provider 400.

These tests pin the transport contract: the publish-side helper carries
``approval_decision`` in the broker payload, and the dispatch-side helper
reconstructs it.
"""

from __future__ import annotations

import pytest

from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import Address
from modex_agent.messaging.broker_bridge import build_input_broker_message
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope


def _session() -> SessionInfo:
    return SessionInfo.from_str("s1.main")


def test_approval_decision_dto_round_trips() -> None:
    """ApprovalDecisionInput serializes to/from a plain dict (broker-safe)."""
    d = ApprovalDecisionInput("call_abc", ApprovalAction.ALLOW)
    assert d.to_dict() == {"tool_call_id": "call_abc", "action": "allow"}
    assert ApprovalDecisionInput.from_dict(d.to_dict()) == d


def test_build_input_broker_message_carries_approval_decision() -> None:
    msg = InputMessage(
        content="",
        session=_session(),
        approval_decision=ApprovalDecisionInput("call_abc", ApprovalAction.DENY),
    )
    broker_msg = build_input_broker_message(msg, Address(kind="agent", name="main"))
    assert broker_msg.payload["approval_decision"] == {
        "tool_call_id": "call_abc",
        "action": "deny",
    }


def test_build_input_broker_message_omits_approval_decision_when_absent() -> None:
    msg = InputMessage(content="hello", session=_session())
    broker_msg = build_input_broker_message(msg, Address(kind="agent", name="main"))
    # No approval_decision -> key absent (not None) so the dispatch side treats
    # it as a normal user turn.
    assert "approval_decision" not in broker_msg.payload


def test_approval_decision_survives_full_broker_round_trip() -> None:
    original = InputMessage(
        content="",
        session=_session(),
        approval_decision=ApprovalDecisionInput("call_xyz", ApprovalAction.ALLOW),
    )
    broker_msg = build_input_broker_message(original, Address(kind="agent", name="main"))
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None

    reconstructed = input_message_from_dispatch_envelope(envelope, session=_session())
    assert reconstructed.approval_decision == ApprovalDecisionInput(
        "call_xyz", ApprovalAction.ALLOW
    )


def test_normal_message_round_trips_without_approval_decision() -> None:
    original = InputMessage(content="hi there", session=_session())
    broker_msg = build_input_broker_message(original, Address(kind="agent", name="main"))
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None

    reconstructed = input_message_from_dispatch_envelope(envelope, session=_session())
    assert reconstructed.approval_decision is None
    assert reconstructed.content == "hi there"
