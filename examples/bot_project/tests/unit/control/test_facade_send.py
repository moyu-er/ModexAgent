"""Seam 1 tests — :class:`BotControlFacade.send()`.

Tests the facade in isolation: mocked ``AgentCommunicationService._send``
returns a sample :class:`AgentSendResult`, mocked ``CommunicationTargetStore``
returns a sample :class:`CommunicationTarget`, mocked workspace resolver
returns mock resources. Verifies the facade produces the correct
:class:`SendResult` for each dispatch strategy (peer normal, parent reply,
native subagent, external subagent) and rejects self-send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.control.facade import BotControlFacade, ControlFacadeError
from bot.control.models import (
    AgentSessionRef,
    DispatchOutcome,
    SendRequest,
)

from modex_agent.core.agent import AgentCommKind, ExecutionStrategyKind
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SESSION_ID = "conv123.main"
_AGENT_NAME = "main"
_POOL = "default"
_WORKSPACE = Path("/home/user/project")
_TARGET_AGENT = "coder"
_TARGET_POOL = "coder_pool"


def _make_request(
    *,
    target_agent: str = _TARGET_AGENT,
    comm_kind: str = "normal",
    parent_session_id: str | None = None,
    content: str = "hello",
    invocation_id: str | None = None,
    graph_instance_id: int | None = None,
) -> SendRequest:
    return SendRequest(
        caller=AgentSessionRef(
            workspace=_WORKSPACE,
            pool=_POOL,
            session_id=_SESSION_ID,
            agent_name=_AGENT_NAME,
        ),
        comm_kind=comm_kind,
        parent_session_id=parent_session_id,
        target_agent=target_agent,
        content=content,
        invocation_id=invocation_id,
        graph_instance_id=graph_instance_id,
    )


def _make_target(
    *,
    kind: AgentCommKind = AgentCommKind.NORMAL,
    tree_ref: SessionTreeManager | None = None,
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
    name: str = _TARGET_AGENT,
) -> CommunicationTarget:
    return CommunicationTarget(
        name=name,
        kind=kind,
        pool_name=_TARGET_POOL,
        tree_ref=tree_ref,
        execution_strategy=execution_strategy,
    )


def _make_send_result(
    *,
    target_agent: str = _TARGET_AGENT,
    target_kind: AgentCommKind = AgentCommKind.NORMAL,
    session_id: str = "conv123.coder",
    invocation_id: str | None = "inv456",
    created_new_task: bool = True,
    is_peer_send: bool = False,
    trace_dir: Path | None = Path("/data/trace"),
) -> AgentSendResult:
    return AgentSendResult(
        target_agent=target_agent,
        target_kind=target_kind,
        session_id=session_id,
        invocation_id=invocation_id,
        created_new_task=created_new_task,
        is_peer_send=is_peer_send,
        trace_dir=trace_dir,
    )


def _make_facade(
    *,
    target: CommunicationTarget | None = None,
    send_result: AgentSendResult | None = None,
    target_not_found: bool = False,
    pool_not_materialized: bool = False,
    session_exists: bool = False,
    root_agent_name: str = "main",
) -> tuple[BotControlFacade, AsyncMock]:
    """Build a facade with mocked dependencies for send.

    Returns ``(facade, mock_send)`` so the test can assert on the
    ``_send`` call. ``session_exists`` controls the
    ``SessionStore.get`` return for the T07 invocation existence check.
    """
    tgt = target if target is not None else _make_target()

    mock_target_store = MagicMock(spec=CommunicationTargetStore)
    if target_not_found:
        mock_target_store.get = MagicMock(return_value=None)
    else:
        mock_target_store.get = MagicMock(return_value=tgt)

    mock_pool_instance = MagicMock()
    mock_pool_instance.target_store = mock_target_store
    mock_pool_instance.root_agent_name = root_agent_name

    mock_resources = MagicMock()
    mock_resources.target = _WORKSPACE
    if pool_not_materialized:
        mock_resources.pools = {}
    else:
        mock_resources.pools = {_POOL: mock_pool_instance}

    mock_session_store = MagicMock()
    mock_session_store.get = AsyncMock(
        return_value=MagicMock() if session_exists else None
    )
    mock_resources.session_index_store = mock_session_store

    mock_service = MagicMock()
    result = send_result if send_result is not None else _make_send_result()
    mock_service._send = AsyncMock(return_value=result)

    async def _workspace_resolver(_root: Path) -> Any:  # noqa: ANN401
        return mock_resources

    async def _message_store_provider(_scope: Any, _res: Any) -> Any:  # noqa: ANN401
        return MagicMock()

    async def _transcript_store_provider(_res: Any) -> Any:  # noqa: ANN401
        return MagicMock()

    async def _comm_service_provider(_res: Any, _pool: str) -> Any:  # noqa: ANN401
        return mock_service

    facade = BotControlFacade(
        workspace_resolver=_workspace_resolver,
        message_store_provider=_message_store_provider,
        transcript_store_provider=_transcript_store_provider,
        communication_service_provider=_comm_service_provider,
        home_root=_WORKSPACE,
        relative_base=None,
    )
    return facade, mock_service


# ---------------------------------------------------------------------------
# Self-send rejection
# ---------------------------------------------------------------------------


class TestSelfSendRejected:
    @pytest.mark.asyncio
    async def test_self_send_raises_422(self) -> None:
        facade, mock_service = _make_facade()
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.send(_make_request(target_agent=_AGENT_NAME))
        assert exc_info.value.status == 422
        assert exc_info.value.error.code == "self_send_rejected"
        message = exc_info.value.error.message
        assert f"You are {_AGENT_NAME!r}" in message
        assert "cannot send a message to yourself" in message
        mock_service._send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_send_checked_before_service_call(self) -> None:
        facade, mock_service = _make_facade()
        with pytest.raises(ControlFacadeError):
            await facade.send(_make_request(target_agent=_AGENT_NAME))
        mock_service._send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Target not found
# ---------------------------------------------------------------------------


class TestTargetNotFound:
    @pytest.mark.asyncio
    async def test_missing_target_raises_404(self) -> None:
        facade, _ = _make_facade(target_not_found=True, root_agent_name="main")
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.send(_make_request(target_agent="nonexistent"))
        assert exc_info.value.status == 404
        assert exc_info.value.error.code == "target_not_found"

    @pytest.mark.asyncio
    async def test_main_agent_fallback_when_not_in_target_store(self) -> None:
        """Subagent sends to its parent (the pool's main agent).

        The main agent is intentionally excluded from the target store
        (it's the sender for send_to_agent). But subagents need to reply
        to their parent via modexctl send. When the target name matches
        the pool's root_agent_name, the facade synthesizes a NORMAL
        same-pool target instead of returning 404.
        """
        send_result = _make_send_result(
            target_kind=AgentCommKind.NORMAL,
            is_peer_send=False,
        )
        facade, mock_service = _make_facade(
            target_not_found=True,
            root_agent_name="main",
            send_result=send_result,
        )
        request = SendRequest(
            caller=AgentSessionRef(
                workspace=_WORKSPACE,
                pool=_POOL,
                session_id="inv1.office-expert",
                agent_name="office-expert",
            ),
            comm_kind="subagent",
            parent_session_id="conv1.main",
            target_agent="main",
            content="reply to parent",
            invocation_id=None,
        )
        await facade.send(request)
        mock_service._send.assert_awaited_once()


# ---------------------------------------------------------------------------
# Peer normal send
# ---------------------------------------------------------------------------


class TestPeerNormalSend:
    @pytest.mark.asyncio
    async def test_peer_send_maps_to_not_applicable(self) -> None:
        target = _make_target(
            kind=AgentCommKind.NORMAL,
            tree_ref=MagicMock(),
            execution_strategy=ExecutionStrategyKind.REACT,
        )
        result = _make_send_result(
            target_kind=AgentCommKind.NORMAL,
            is_peer_send=True,
            created_new_task=False,
            invocation_id=None,
            trace_dir=None,
        )
        facade, _ = _make_facade(target=target, send_result=result)
        send_result = await facade.send(_make_request())

        assert send_result.dispatch_outcome == DispatchOutcome.NOT_APPLICABLE
        assert send_result.is_peer_send is True
        assert send_result.is_external_target is False
        assert send_result.trace_dir is None
        assert send_result.invocation_id is None

    @pytest.mark.asyncio
    async def test_peer_send_passes_correct_args_to_service(self) -> None:
        target = _make_target(tree_ref=MagicMock())
        result = _make_send_result(is_peer_send=True, created_new_task=False)
        facade, mock_service = _make_facade(target=target, send_result=result)
        await facade.send(_make_request(content="test message"))

        mock_service._send.assert_awaited_once()
        call_kwargs = mock_service._send.call_args.kwargs
        assert call_kwargs["target"] is target
        assert call_kwargs["content"] == "test message"
        assert call_kwargs["invocation_id"] is None
        assert call_kwargs["context"].comm_kind == AgentCommKind.NORMAL


# ---------------------------------------------------------------------------
# Parent reply
# ---------------------------------------------------------------------------


class TestParentReply:
    @pytest.mark.asyncio
    async def test_parent_reply_maps_to_not_applicable(self) -> None:
        target = _make_target(kind=AgentCommKind.NORMAL, tree_ref=None)
        result = _make_send_result(
            target_kind=AgentCommKind.NORMAL,
            is_peer_send=False,
            created_new_task=False,
            invocation_id=None,
            trace_dir=None,
            session_id="parent.session",
        )
        facade, _ = _make_facade(target=target, send_result=result)
        send_result = await facade.send(
            _make_request(comm_kind="subagent", parent_session_id="parent.session")
        )

        assert send_result.dispatch_outcome == DispatchOutcome.NOT_APPLICABLE
        assert send_result.is_peer_send is False
        assert send_result.session_id == "parent.session"


# ---------------------------------------------------------------------------
# Native subagent dispatch
# ---------------------------------------------------------------------------


class TestNativeSubagentDispatch:
    @pytest.mark.asyncio
    async def test_subagent_dispatch_maps_to_new_task(self) -> None:
        target = _make_target(
            kind=AgentCommKind.SUBAGENT,
            execution_strategy=ExecutionStrategyKind.REACT,
        )
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="inv789",
            session_id="inv789.coder",
        )
        facade, _ = _make_facade(target=target, send_result=result)
        send_result = await facade.send(_make_request())

        assert send_result.dispatch_outcome == DispatchOutcome.NEW_TASK
        assert send_result.is_peer_send is False
        assert send_result.is_external_target is False
        assert send_result.invocation_id == "inv789"
        assert send_result.session_id == "inv789.coder"
        assert send_result.trace_dir == Path("/data/trace")


# ---------------------------------------------------------------------------
# External subagent dispatch
# ---------------------------------------------------------------------------


class TestExternalSubagentDispatch:
    @pytest.mark.asyncio
    async def test_external_target_derived_from_execution_strategy(self) -> None:
        target = _make_target(
            kind=AgentCommKind.SUBAGENT,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="inv999",
        )
        facade, _ = _make_facade(target=target, send_result=result)
        send_result = await facade.send(_make_request())

        assert send_result.dispatch_outcome == DispatchOutcome.NEW_TASK
        assert send_result.is_external_target is True
        assert send_result.is_peer_send is False


# ---------------------------------------------------------------------------
# AgentContext construction
# ---------------------------------------------------------------------------


class TestAgentContext:
    @pytest.mark.asyncio
    async def test_session_info_from_str(self) -> None:
        facade, mock_service = _make_facade()
        await facade.send(_make_request())

        context = mock_service._send.call_args.kwargs["context"]
        assert context.session.session_id == _SESSION_ID
        assert context.session.agent_name == _AGENT_NAME

    @pytest.mark.asyncio
    async def test_parent_session_id_set_for_subagent(self) -> None:
        facade, mock_service = _make_facade()
        await facade.send(
            _make_request(
                comm_kind="subagent",
                parent_session_id="parent.session",
            )
        )

        context = mock_service._send.call_args.kwargs["context"]
        assert context.comm_kind == AgentCommKind.SUBAGENT
        assert context.session.parent_session_id == "parent.session"

    @pytest.mark.asyncio
    async def test_comm_kind_passed_through(self) -> None:
        facade, mock_service = _make_facade()
        await facade.send(_make_request(comm_kind="normal"))

        context = mock_service._send.call_args.kwargs["context"]
        assert context.comm_kind == AgentCommKind.NORMAL

    @pytest.mark.asyncio
    async def test_invocation_id_none_passed_through(self) -> None:
        facade, mock_service = _make_facade()
        await facade.send(_make_request())

        invocation_id = mock_service._send.call_args.kwargs["invocation_id"]
        assert invocation_id is None


# ---------------------------------------------------------------------------
# Invalid comm_kind
# ---------------------------------------------------------------------------


class TestInvalidCommKind:
    @pytest.mark.asyncio
    async def test_invalid_comm_kind_raises_400(self) -> None:
        facade, _ = _make_facade()
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.send(_make_request(comm_kind="invalid"))
        assert exc_info.value.status == 400
        assert exc_info.value.error.code == "invalid_comm_kind"


# ---------------------------------------------------------------------------
# Pool not materialized
# ---------------------------------------------------------------------------


class TestPoolNotMaterialized:
    @pytest.mark.asyncio
    async def test_pool_not_in_resources_raises_404(self) -> None:
        facade, _ = _make_facade(pool_not_materialized=True)
        with pytest.raises(ControlFacadeError) as exc_info:
            await facade.send(_make_request())
        assert exc_info.value.status == 404
        assert exc_info.value.error.code == "pool_not_found"


# ---------------------------------------------------------------------------
# T07 — invocation-id existence check
# ---------------------------------------------------------------------------


class TestInvocationIdExistence:
    @pytest.mark.asyncio
    async def test_existing_invocation_returns_resumed(self) -> None:
        target = _make_target(kind=AgentCommKind.SUBAGENT)
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="inv123",
            session_id="inv123.coder",
        )
        facade, _ = _make_facade(
            target=target, send_result=result, session_exists=True
        )
        send_result = await facade.send(_make_request(invocation_id="inv123"))

        assert send_result.dispatch_outcome == DispatchOutcome.RESUMED
        assert send_result.invocation_id == "inv123"
        assert send_result.requested_invocation_id is None

    @pytest.mark.asyncio
    async def test_nonexistent_invocation_returns_requested_not_found(self) -> None:
        target = _make_target(kind=AgentCommKind.SUBAGENT)
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="newmint1",
            session_id="newmint1.coder",
        )
        facade, _ = _make_facade(
            target=target, send_result=result, session_exists=False
        )
        send_result = await facade.send(_make_request(invocation_id="inv123"))

        assert (
            send_result.dispatch_outcome
            == DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND
        )
        assert send_result.requested_invocation_id == "inv123"
        assert send_result.invocation_id == "newmint1"

    @pytest.mark.asyncio
    async def test_existing_invocation_passes_requested_id_to_service(self) -> None:
        target = _make_target(kind=AgentCommKind.SUBAGENT)
        result = _make_send_result(target_kind=AgentCommKind.SUBAGENT)
        facade, mock_service = _make_facade(
            target=target, send_result=result, session_exists=True
        )
        await facade.send(_make_request(invocation_id="inv123"))

        invocation_id = mock_service._send.call_args.kwargs["invocation_id"]
        assert invocation_id == "inv123"

    @pytest.mark.asyncio
    async def test_nonexistent_invocation_passes_new_id_to_service(self) -> None:
        target = _make_target(kind=AgentCommKind.SUBAGENT)
        result = _make_send_result(target_kind=AgentCommKind.SUBAGENT)
        facade, mock_service = _make_facade(
            target=target, send_result=result, session_exists=False
        )
        await facade.send(_make_request(invocation_id="inv123"))

        invocation_id = mock_service._send.call_args.kwargs["invocation_id"]
        assert invocation_id != "inv123"
        assert len(invocation_id) == 8

    @pytest.mark.asyncio
    async def test_no_invocation_id_does_not_check_session_store(self) -> None:
        target = _make_target(kind=AgentCommKind.SUBAGENT)
        result = _make_send_result(target_kind=AgentCommKind.SUBAGENT)
        facade, mock_service = _make_facade(
            target=target, send_result=result, session_exists=False
        )
        send_result = await facade.send(_make_request(invocation_id=None))

        assert send_result.dispatch_outcome == DispatchOutcome.NEW_TASK
        assert send_result.requested_invocation_id is None
        invocation_id = mock_service._send.call_args.kwargs["invocation_id"]
        assert invocation_id is None

    @pytest.mark.asyncio
    async def test_peer_send_does_not_check_session_store(self) -> None:
        target = _make_target(
            kind=AgentCommKind.NORMAL, tree_ref=MagicMock()
        )
        result = _make_send_result(
            target_kind=AgentCommKind.NORMAL,
            is_peer_send=True,
            created_new_task=False,
            invocation_id=None,
        )
        facade, _ = _make_facade(
            target=target, send_result=result, session_exists=True
        )
        send_result = await facade.send(_make_request(invocation_id="inv123"))

        assert send_result.dispatch_outcome == DispatchOutcome.NOT_APPLICABLE
        assert send_result.is_peer_send is True

    @pytest.mark.asyncio
    async def test_parent_reply_does_not_check_session_store(self) -> None:
        target = _make_target(kind=AgentCommKind.NORMAL, tree_ref=None)
        result = _make_send_result(
            target_kind=AgentCommKind.NORMAL,
            is_peer_send=False,
            created_new_task=False,
            invocation_id=None,
        )
        facade, _ = _make_facade(
            target=target, send_result=result, session_exists=True
        )
        send_result = await facade.send(
            _make_request(
                comm_kind="subagent",
                parent_session_id="parent.session",
                invocation_id="inv123",
            )
        )

        assert send_result.dispatch_outcome == DispatchOutcome.NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_external_subagent_with_invocation_id_still_checks(self) -> None:
        target = _make_target(
            kind=AgentCommKind.SUBAGENT,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="inv123",
        )
        facade, _ = _make_facade(
            target=target, send_result=result, session_exists=True
        )
        send_result = await facade.send(_make_request(invocation_id="inv123"))

        assert send_result.dispatch_outcome == DispatchOutcome.RESUMED
        assert send_result.is_external_target is True


# ---------------------------------------------------------------------------
# graph_instance_id propagation (Site 4)
# ---------------------------------------------------------------------------


class TestSendRequestGraphInstanceId:
    def test_graph_instance_id_defaults_to_none(self) -> None:
        request = _make_request()
        assert request.graph_instance_id is None

    def test_graph_instance_id_accepted(self) -> None:
        request = _make_request(graph_instance_id=42)
        assert request.graph_instance_id == 42

    def test_graph_instance_id_rejects_extra_fields(self) -> None:
        """SendRequest is frozen + extra='forbid' — unknown keys rejected."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SendRequest(
                caller=AgentSessionRef(
                    workspace=_WORKSPACE,
                    pool=_POOL,
                    session_id=_SESSION_ID,
                    agent_name=_AGENT_NAME,
                ),
                comm_kind="normal",
                target_agent=_TARGET_AGENT,
                content="hello",
                graph_instance_id=1,
                unknown_extra="rejected",  # type: ignore[call-arg]
            )


class TestGraphInstanceIdPropagation:
    @pytest.mark.asyncio
    async def test_graph_instance_id_propagates_to_agent_context(self) -> None:
        """When SendRequest carries graph_instance_id, facade sets it on AgentContext."""
        facade, mock_service = _make_facade()
        await facade.send(_make_request(graph_instance_id=42))

        context = mock_service._send.call_args.kwargs["context"]
        assert context.graph_instance_id == 42

    @pytest.mark.asyncio
    async def test_graph_instance_id_none_propagates_when_omitted(self) -> None:
        """When SendRequest omits graph_instance_id, AgentContext.graph_instance_id is None."""
        facade, mock_service = _make_facade()
        await facade.send(_make_request())

        context = mock_service._send.call_args.kwargs["context"]
        assert context.graph_instance_id is None

    @pytest.mark.asyncio
    async def test_graph_instance_id_propagates_for_subagent_dispatch(self) -> None:
        """graph_instance_id propagates through the subagent dispatch path too."""
        target = _make_target(
            kind=AgentCommKind.SUBAGENT,
            execution_strategy=ExecutionStrategyKind.REACT,
        )
        result = _make_send_result(
            target_kind=AgentCommKind.SUBAGENT,
            created_new_task=True,
            invocation_id="inv-graph",
            session_id="inv-graph.coder",
        )
        facade, mock_service = _make_facade(target=target, send_result=result)
        await facade.send(_make_request(graph_instance_id=7))

        context = mock_service._send.call_args.kwargs["context"]
        assert context.graph_instance_id == 7
