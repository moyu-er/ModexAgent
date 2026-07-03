"""Tier 1 — approval_decision transport through ``PoolRouter._route_to_pool``.

The production webui approval path is (poll-driven):

  POST /approvals -> webui_pipeline (EnqueueStage lifts ``approval_decision``
  onto ``InputMessage``) -> WS adapter queue -> ``WorkspaceMessageDispatcher``
  -> ``PoolRouter.route_message`` -> ``_route_to_pool`` ->
  ``pool.pool.submit_input(sid, InputMessage)`` ->
  ``AgentPool.submit_input`` serializes via ``BrokerInputPayload`` ->
  ``input_message_from_dispatch_envelope`` reconstructs the ``InputMessage``
  -> ``build_turn_request`` short-circuit -> resume.

``_route_to_pool`` no longer hand-builds a broker payload; it hands the full
``InputMessage`` to ``submit_input``. These tests pin the decision's survival at
that hand-off and through the broker serialization round-trip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bot.service.pool_router import PoolRouter, PoolSessionStore
from modex_agent.approval.types import ApprovalAction
from modex_agent.approval.views import ApprovalDecisionInput
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.messaging.broker_bridge import BrokerInputPayload
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.pool import input_message_from_dispatch_envelope


class _MockBroker:
    """PoolRouter still takes a broker; route_message no longer sends through it
    (it goes via pool.pool.submit_input), but the ctor arg remains."""

    async def send_to(self, address: Any, msg: Any) -> None:  # noqa: ARG002
        pass


class _MockPool:
    """Records the InputMessage handed to ``pool.pool.submit_input``."""

    main_agent_name = "main"
    main_address = "pool:main"

    def __init__(self) -> None:
        self.submitted: list[tuple[str, InputMessage]] = []

        class _Inner:
            async def submit_input(inner_self, sid: str, msg: InputMessage) -> None:
                self.submitted.append((sid, msg))

        self.pool = _Inner()


def _router(tmp_path: Path, pool: _MockPool) -> PoolRouter:
    session_store = PoolSessionStore(tmp_path)
    # Seed the session->pool mapping so route_message resolves to our pool.
    session_store.set("sess", "main")
    return PoolRouter(
        input_adapter=None,  # type: ignore[arg-type]  # route_message doesn't read it
        broker=_MockBroker(),
        pools={"main": pool},
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
    """A webui ALLOW decision must reach submit_input on the InputMessage."""
    pool = _MockPool()
    router = _router(tmp_path, pool)

    await router.route_message(_approval_msg(ApprovalAction.ALLOW, "c1"))

    assert len(pool.submitted) == 1
    decision = pool.submitted[0][1].approval_decision
    assert decision is not None
    assert decision.tool_call_id == "c1"
    assert decision.action == ApprovalAction.ALLOW


@pytest.mark.asyncio
async def test_route_message_carries_deny_decision_in_payload(tmp_path: Path) -> None:
    pool = _MockPool()
    router = _router(tmp_path, pool)

    await router.route_message(_approval_msg(ApprovalAction.DENY, "c2"))

    decision = pool.submitted[0][1].approval_decision
    assert decision is not None
    assert decision.tool_call_id == "c2"
    assert decision.action == ApprovalAction.DENY


@pytest.mark.asyncio
async def test_decision_survives_full_dispatch_reconstruction(tmp_path: Path) -> None:
    """The decision must survive the BrokerInputPayload -> envelope ->
    InputMessage rebuild that ``AgentPool.submit_input`` +
    ``input_message_from_dispatch_envelope`` perform. If the field is dropped
    here, ``build_turn_request`` treats it as an empty user turn (bug #2)."""
    pool = _MockPool()
    router = _router(tmp_path, pool)

    await router.route_message(_approval_msg(ApprovalAction.ALLOW, "call_abc"))

    submitted = pool.submitted[0][1]
    # Mirror AgentPool.submit_input's serialization into the dispatch envelope.
    payload_model = BrokerInputPayload(
        content=submitted.content,
        session_id=submitted.session.session_id_prefix,
        agent_session_id="sess.main",
        metadata=dict(submitted.metadata) if submitted.metadata else {},
        sender_id=submitted.sender_id,
        chat_id=submitted.chat_id,
        approval_decision=submitted.approval_decision.to_dict()
        if submitted.approval_decision is not None
        else None,
        attachments_resolved=[a.to_dict() for a in submitted.attachments_resolved],
        message_type="external_input",
    )
    envelope = AgentMessageEnvelope(
        payload=payload_model.model_dump(exclude_none=True),
        source=AgentAddress(kind="channel", name="websocket"),
        target=AgentAddress(kind="agent", name="main"),
        message_type="external_input",
        session_id=submitted.session.session_id_prefix,
        agent_session_id="sess.main",
    )

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

    It must keep working (content reaches submit_input) and must NOT carry an
    approval_decision. Green contrast proving the structured-decision path is
    the only one that sets the field."""
    pool = _MockPool()
    router = _router(tmp_path, pool)

    await router.route_message(
        InputMessage(
            content="/approve",
            session=SessionInfo(session_id="sess.main", agent_name="main"),
            source="qq",
            channel="qq",
        )
    )

    submitted = pool.submitted[0][1]
    assert submitted.content == "/approve"
    assert submitted.approval_decision is None


@pytest.mark.asyncio
async def test_normal_message_omits_approval_decision(tmp_path: Path) -> None:
    """Ordinary messages must not gain an approval_decision."""
    pool = _MockPool()
    router = _router(tmp_path, pool)

    await router.route_message(
        InputMessage(
            content="hello",
            session=SessionInfo(session_id="sess.main", agent_name="main"),
            source="websocket",
            channel="websocket",
        )
    )

    submitted = pool.submitted[0][1]
    assert submitted.content == "hello"
    assert submitted.approval_decision is None
