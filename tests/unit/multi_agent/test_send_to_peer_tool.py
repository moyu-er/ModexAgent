from __future__ import annotations

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToPeerTool,
)
from modex_agent.tools.manager import InMemoryToolManager


class _RecordingService:
    def __init__(self) -> None:
        self.async_invocation_id: str | None = None
        self.last_target: CommunicationTarget | None = None
        self.last_content: str | None = None

    async def send_async(
        self,
        *,
        target: CommunicationTarget,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        self.async_invocation_id = invocation_id
        self.last_target = target
        self.last_content = content
        return "ok"


def _context() -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=object(),  # type: ignore[arg-type]
        tool_manager=object(),  # type: ignore[arg-type]
        session=SessionInfo.from_str("test.agent"),
    )


def _store_with_peer() -> CommunicationTargetStore:
    store = CommunicationTargetStore()
    store.add(
        CommunicationTarget(
            name="opencode",
            kind=AgentCommKind.NORMAL,
            description="Independent coding assistant",
        )
    )
    return store


def _tool(
    store: CommunicationTargetStore,
    service: _RecordingService | None = None,
) -> SendToPeerTool:
    return SendToPeerTool(
        store=store,
        source=AgentAddress(name="test"),
        service=service or _RecordingService(),  # type: ignore[arg-type]
    )


class TestSendToPeerToolName:
    def test_name_is_send_to_peer(self) -> None:
        assert _tool(CommunicationTargetStore()).name == "send_to_peer"


class TestSendToPeerToolParams:
    def test_params_have_target_peer_and_content_only(self) -> None:
        tool = _tool(CommunicationTargetStore())
        props = tool.parameters["properties"]
        assert "target_peer" in props
        assert "content" in props
        assert "invocation_id" not in props
        assert tool.parameters["required"] == ["target_peer", "content"]


class TestSendToPeerToolDynamicSchema:
    def test_enum_includes_only_peers(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="opencode", kind=AgentCommKind.NORMAL))
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        tool = _tool(store)
        schema = tool.get_dynamic_schema()
        target_schema = schema["function"]["parameters"]["properties"]["target_peer"]
        assert target_schema.get("enum") == ["opencode"]


class TestSendToPeerToolExecute:
    @pytest.mark.asyncio
    async def test_execute_sends_peer_with_no_invocation_id(self) -> None:
        service = _RecordingService()
        tool = _tool(_store_with_peer(), service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(target_peer="opencode", content="hello")
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.last_target is not None
        assert service.last_target.name == "opencode"
        assert service.last_content == "hello"
        assert service.async_invocation_id is None

    @pytest.mark.asyncio
    async def test_execute_rejects_subagent_target(self) -> None:
        service = _RecordingService()
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        tool = _tool(store, service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(target_peer="scout", content="test")
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "scout" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_execute_rejects_unknown_peer(self) -> None:
        service = _RecordingService()
        tool = _tool(_store_with_peer(), service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(target_peer="nope", content="test")
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_self_send_rejected(self) -> None:
        service = _RecordingService()
        tool = _tool(_store_with_peer(), service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(target_peer="agent", content="hi")
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "yourself" in result
        assert service.last_target is None


class TestSendToPeerToolDescription:
    def test_description_defines_peer_as_independent_not_worker(self) -> None:
        desc = _tool(_store_with_peer()).description.lower()
        assert "independent assistant" in desc
        assert "not a worker" in desc
        assert "communication" in desc
        assert "task delegation" in desc

    def test_description_prefers_subagent(self) -> None:
        desc = _tool(_store_with_peer()).description.lower()
        assert "subagent" in desc
        assert "task" in desc

    def test_description_avoids_implementation_details(self) -> None:
        desc = _tool(_store_with_peer()).description.lower()
        assert "pool" not in desc
        assert "main agent" not in desc
        assert "tree" not in desc

    def test_description_no_peers_available(self) -> None:
        desc = _tool(CommunicationTargetStore()).description
        assert "No peers currently available" in desc

    def test_description_frames_async_as_communication_channel(self) -> None:
        desc = _tool(_store_with_peer()).description
        assert "communication channel" in desc


class TestSendToPeerToolGraphMode:
    def test_list_targets_empty_in_graph_mode(self) -> None:
        from modex_agent.memory.history import ListMessageHistory

        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="opencode", kind=AgentCommKind.NORMAL))
        tool = _tool(store)

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("conv-1.researcher"),
            graph_instance_id=42,
        )
        token = current_agent_context.set(ctx)
        try:
            assert tool.list_targets() == []
        finally:
            current_agent_context.reset(token)
