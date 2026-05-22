"""Tests for SendToAgentTool and SendToAgentAsyncTool."""

from __future__ import annotations

import pytest

from framework.core.agent import AgentContext, current_agent_context
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.registry import AgentProfile
from framework.multi_agent.tools import (
    ListCommunicationTargetsTool,
    SendToAgentAsyncTool,
    SendToAgentTool,
)


class _RecordingService:
    def __init__(self) -> None:
        self.sync_invocation_id: str | None = None
        self.async_invocation_id: str | None = None

    async def send_sync(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        _ = target_agent, content, context
        self.sync_invocation_id = invocation_id
        return "ok"

    async def send_async(
        self,
        *,
        target_agent: str,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        _ = target_agent, content, context
        self.async_invocation_id = invocation_id
        return "ok"

    def build_targets_description(self) -> str:
        return "Available targets:\n- office-expert (subagent)"


class _Registry:
    def __init__(self, profiles: list[AgentProfile]) -> None:
        self._profiles = profiles

    def list_profiles(self, caller: str | None = None) -> list[AgentProfile]:
        _ = caller
        return self._profiles


def _context() -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=object(),  # type: ignore[arg-type]
        tool_manager=object(),  # type: ignore[arg-type]
    )


class TestSendToAgentToolNames:
    def test_old_tool_names_are_absent(self) -> None:
        """Old tools must not be importable from tools module."""
        import framework.multi_agent.tools as t

        assert not hasattr(t, "DispatchTaskTool"), "DispatchTaskTool should be removed"
        assert not hasattr(t, "SendMessageTool"), "SendMessageTool should be removed"
        assert not hasattr(t, "SendMessageAsyncTool"), "SendMessageAsyncTool should be removed"


class TestNewToolExports:
    def test_send_to_agent_tool_importable(self) -> None:
        from framework.multi_agent.tools import SendToAgentTool

        assert SendToAgentTool.__name__ == "SendToAgentTool"

    def test_send_to_agent_async_tool_importable(self) -> None:
        from framework.multi_agent.tools import SendToAgentAsyncTool

        assert SendToAgentAsyncTool.__name__ == "SendToAgentAsyncTool"

    def test_new_tools_exported_from_multi_agent(self) -> None:
        from framework.multi_agent import SendToAgentAsyncTool, SendToAgentTool

        assert SendToAgentTool is not None
        assert SendToAgentAsyncTool is not None


class TestListCommunicationTargetsTool:
    @pytest.mark.asyncio
    async def test_subagent_lists_only_normal_reply_targets(self) -> None:
        registry = _Registry([
            AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
            AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT),
            AgentProfile(name="query-12306", comm_kind=AgentCommKind.SUBAGENT),
        ])
        tool = ListCommunicationTargetsTool(
            self_address=AgentAddress(name="office-expert"),
            registry=registry,  # type: ignore[arg-type]
        )

        result = await tool.execute()

        assert "main" in result
        assert "query-12306" not in result


class TestSchema:
    def test_sync_tool_has_required_invocation_id(self) -> None:
        service = _RecordingService()
        tool = SendToAgentTool(
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )

        assert "invocation_id" in tool.parameters["properties"]
        assert "invocation_id" in tool.parameters["required"]

    def test_async_tool_has_required_invocation_id(self) -> None:
        service = _RecordingService()
        tool = SendToAgentAsyncTool(
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )

        assert "invocation_id" in tool.parameters["properties"]
        assert "invocation_id" in tool.parameters["required"]


class TestToolInvocationIdForwarding:
    @pytest.mark.asyncio
    async def test_sync_tool_forwards_invocation_id_to_service(self) -> None:
        service = _RecordingService()
        tool = SendToAgentTool(
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )

        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="continue task",
                invocation_id="task-123",
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.sync_invocation_id == "task-123"

    @pytest.mark.asyncio
    async def test_async_tool_forwards_invocation_id_to_service(self) -> None:
        service = _RecordingService()
        tool = SendToAgentAsyncTool(
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )

        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="start task",
                invocation_id="",
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.async_invocation_id == ""
