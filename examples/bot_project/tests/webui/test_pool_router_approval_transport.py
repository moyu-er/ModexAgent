"""Tier 1 — approval_decision transport through ``PoolRouter._route_to_pool``.

The production webui approval path is:

  POST /approvals -> webui_pipeline (EnqueueStage lifts ``approval_decision``
  onto ``InputMessage``) -> WS adapter queue -> ``WorkspaceMessageDispatcher``
  -> ``PoolRouter.route_message`` -> ``_route_to_pool`` -> broker ->
  ``AgentPool._dispatch_agent_message`` ->
  ``input_message_from_dispatch_envelope`` reconstructs the ``InputMessage``
  -> ``build_turn_request`` short-circuit -> resume.

``BrokerBridgeService`` is wired with ``input_bindings={}`` in production
(``pool_builder.py``), so ``build_input_broker_message`` (broker_bridge.py) is
NEVER called on the webui path — the only broker hop is the one
``PoolRouter._route_to_pool`` builds by hand. If that hand-built
``BrokerMessage.payload`` omits ``approval_decision``, the decision is lost in
transit, arrives as an empty user turn, leaves a dangling assistant
``tool_calls``, and the provider returns 400.

These tests pin the field's survival at the exact hand-off point that previous
unit tests (which drove ``pipeline._process_message`` directly) missed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.service.pool_router import PoolRouter, PoolSessionStore
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker import BrokerMessage
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput


class _MockBroker:
    """Captures every BrokerMessage sent_to a pool address."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, BrokerMessage]] = []

    async def send_to(self, address: Any, msg: BrokerMessage) -> None:
        self.sent.append((address, msg))


class _MockPool:
    main_agent_name = "main"
    main_address = "pool:main"


def _router(tmp_path: Path, broker: _MockBroker) -> PoolRouter:
    session_store = PoolSessionStore(tmp_path)
    # Seed the session->pool mapping so route_message resolves to our pool.
    session_store.set("sess", "main")
    return PoolRouter(
        input_adapter=None,  # type: ignore[arg-type]  # route_message doesn't read it
        broker=broker,
        pools={"main": _MockPool()},
        session_store=session_store,
        default_pool="main",
    )


def _approval_msg(action: ApprovalAction, tool_call_id: str = "c1") -> InputMessage:
    return InputMessage(
        content="",
        session=SessionInfo(session_id="sess.main", agent_name="main"),
        source="websocket",
        channel="websocket",
        approval_decision=ApprovalDecisionInput(tool_call_id=tool_call_id, action=action),
    )


@pytest.mark.asyncio
async def test_route_message_carries_allow_decision_in_payload(tmp_path: Path) -> None:
    """A webui ALLOW decision must land in the BrokerMessage payload."""
    broker = _MockBroker()
    router = _router(tmp_path, broker)

    await router.route_message(_approval_msg(ApprovalAction.ALLOW, "c1"))

    assert len(broker.sent) == 1
    _, msg = broker.sent[0]
    assert msg.payload.get("approval_decision") == {
        "tool_call_id": "c1",
        "action": "allow",
    }, f"approval_decision lost at PoolRouter._route_to_pool; payload={msg.payload!r}"


@pytest.mark.asyncio
async def test_route_message_carries_deny_decision_in_payload(tmp_path: Path) -> None:
    broker = _MockBroker()
    router = _router(tmp_path, broker)

    await router.route_message(_approval_msg(ApprovalAction.DENY, "c2"))

    _, msg = broker.sent[0]
    assert msg.payload.get("approval_decision") == {
        "tool_call_id": "c2",
        "action": "deny",
    }


@pytest.mark.asyncio
async def test_decision_survives_full_dispatch_reconstruction(tmp_path: Path) -> None:
    """The decision must survive broker -> envelope -> InputMessage rebuild.

    This is the exact reconstruction ``AgentPool._dispatch_agent_message``
    performs via ``input_message_from_dispatch_envelope``. If the field is
    missing here, ``build_turn_request`` never sees it and the resume branch
    never runs.
    """
    broker = _MockBroker()
    router = _router(tmp_path, broker)

    await router.route_message(_approval_msg(ApprovalAction.ALLOW, "call_abc"))

    _, broker_msg = broker.sent[0]
    envelope = AgentMessageEnvelope.from_broker_message(broker_msg)
    assert envelope is not None, "headers must carry session_id/agent_session_id"

    rebuilt = input_message_from_dispatch_envelope(
        envelope,
        session=SessionInfo(session_id="sess.main", agent_name="main"),
        metadata={},
    )
    assert rebuilt.approval_decision is not None, (
        "approval_decision dropped across broker dispatch — build_turn_request "
        "will treat this as an empty user turn (bug #2)"
    )
    assert rebuilt.approval_decision.tool_call_id == "call_abc"
    assert rebuilt.approval_decision.action == ApprovalAction.ALLOW


@pytest.mark.asyncio
async def test_im_approve_text_does_not_need_approval_field(tmp_path: Path) -> None:
    """IM ``/approve`` rides on content text, not the structured field.

    It must keep working (content survives in payload) and must NOT carry an
    approval_decision key. This is the green contrast proving the fix targets
    only the webui structured-decision path.
    """
    broker = _MockBroker()
    router = _router(tmp_path, broker)

    await router.route_message(
        InputMessage(
            content="/approve",
            session=SessionInfo(session_id="sess.main", agent_name="main"),
            source="qq",
            channel="qq",
        )
    )

    _, msg = broker.sent[0]
    assert msg.payload.get("content") == "/approve"
    assert "approval_decision" not in msg.payload


@pytest.mark.asyncio
async def test_normal_message_omits_approval_decision(tmp_path: Path) -> None:
    """Ordinary messages must not gain an approval_decision key."""
    broker = _MockBroker()
    router = _router(tmp_path, broker)

    await router.route_message(
        InputMessage(
            content="hello",
            session=SessionInfo(session_id="sess.main", agent_name="main"),
            source="websocket",
            channel="websocket",
        )
    )

    _, msg = broker.sent[0]
    assert "approval_decision" not in msg.payload
