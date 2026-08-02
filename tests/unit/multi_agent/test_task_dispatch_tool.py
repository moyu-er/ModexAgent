"""Tests for TaskDispatchTool — subagent-only task dispatch.

The task tool is a thin sibling of send_to_agent that always starts a fresh
subagent session (invocation_id=None) and only accepts SUBAGENT targets.
These tests mirror the SendToAgentTool test patterns but assert the
subagent-only constraint and the richer task-prompt description.
"""

from __future__ import annotations

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    TaskDispatchTool,
)


class _RecordingService:
    """Duck-typed AgentCommunicationService stub recording send_async args."""

    def __init__(self) -> None:
        self.async_invocation_id: str | None = None
        self.last_target: CommunicationTarget | None = None
        self.last_content: str | None = None
        self.last_context: AgentContext | None = None

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
        self.last_context = context
        return "ok"


def _context() -> AgentContext:
    """Caller context whose session.agent_name is ``agent`` (from test.agent)."""
    return AgentContext(
        system_prompt="",
        history=object(),  # type: ignore[arg-type]
        tool_manager=object(),  # type: ignore[arg-type]
        session=SessionInfo.from_str("test.agent"),
    )


def _store_with_subagent_target() -> CommunicationTargetStore:
    """Pre-populated store with a single SUBAGENT target."""
    store = CommunicationTargetStore()
    store.add(
        CommunicationTarget(
            name="office-expert",
            kind=AgentCommKind.SUBAGENT,
            description="Office tasks",
        )
    )
    return store


# -- 1. name -----------------------------------------------------------------


class TestTaskDispatchToolName:
    def test_task_tool_name_is_task(self) -> None:
        tool = TaskDispatchTool(
            store=CommunicationTargetStore(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert tool.name == "task"


# -- 2. params ---------------------------------------------------------------


class TestTaskDispatchToolParams:
    def test_params_have_target_agent_and_content_only(self) -> None:
        tool = TaskDispatchTool(
            store=CommunicationTargetStore(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        props = tool.parameters["properties"]
        assert "target_agent" in props
        assert "content" in props
        assert "invocation_id" not in props
        required = tool.parameters["required"]
        assert "target_agent" in required
        assert "content" in required
        assert "invocation_id" not in required


# -- 3, 4, 12. dynamic schema ------------------------------------------------


class TestTaskDispatchToolDynamicSchema:
    def test_target_agent_enum_only_subagent_targets(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        store.add(CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT))
        tool = TaskDispatchTool(
            store=store,
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        target_schema = schema["function"]["parameters"]["properties"]["target_agent"]
        assert target_schema.get("enum") == ["scout", "worker"]

    def test_target_agent_enum_excludes_normal_targets(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        tool = TaskDispatchTool(
            store=store,
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        target_schema = schema["function"]["parameters"]["properties"]["target_agent"]
        enum = target_schema.get("enum")
        assert enum == ["scout"]
        assert "peer-main" not in (enum or [])

    def test_dynamic_schema_not_mutating_static_params(self) -> None:
        store = _store_with_subagent_target()
        tool = TaskDispatchTool(
            store=store,
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        before = tool.parameters["properties"]["target_agent"]
        tool.get_dynamic_schema()
        after = tool.parameters["properties"]["target_agent"]
        assert before is after
        assert "enum" not in before


# -- 5, 6, 7, 13. execute ----------------------------------------------------


class TestTaskDispatchToolExecute:
    @pytest.mark.asyncio
    async def test_execute_calls_send_async_with_invocation_id_none(self) -> None:
        service = _RecordingService()
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="do the thing",
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.async_invocation_id is None
        assert service.last_target is not None
        assert service.last_target.name == "office-expert"
        assert service.last_content == "do the thing"
        assert service.last_context is not None

    @pytest.mark.asyncio
    async def test_execute_rejects_unknown_target(self) -> None:
        service = _RecordingService()
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="nonexistent",
                content="test",
            )
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "nonexistent" in result
        assert "office-expert" in result  # lists available subagents
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_execute_rejects_normal_target(self) -> None:
        service = _RecordingService()
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        tool = TaskDispatchTool(
            store=store,
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="peer-main",
                content="test",
            )
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "task dispatches to subagents only" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_self_dispatch_rejected(self) -> None:
        service = _RecordingService()
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="agent",  # matches caller_name from _context()
                content="hello self",
            )
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "yourself" in result
        assert service.last_target is None


# -- 8, 9, 10, 11. description -----------------------------------------------


class TestTaskDispatchToolDescription:
    def test_description_contains_prompt_construction_guidance(self) -> None:
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        for keyword in ("TASK", "CONTEXT", "SCOPE", "OUTPUT", "VERIFICATION", "BOUNDARIES"):
            assert keyword in desc, f"expected {keyword!r} in description"

    def test_description_contains_when_not_to_use(self) -> None:
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        assert "When NOT to use" in desc
        lowered = desc.lower()
        assert "read" in lowered or "grep" in lowered or "glob" in lowered

    def test_description_contains_concurrency_guidance(self) -> None:
        tool = TaskDispatchTool(
            store=_store_with_subagent_target(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        assert "concurrently" in desc or "multiple" in desc

    def test_description_lists_available_subagents(self) -> None:
        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="scout",
                kind=AgentCommKind.SUBAGENT,
                description="Fast recon",
            )
        )
        store.add(
            CommunicationTarget(
                name="worker",
                kind=AgentCommKind.SUBAGENT,
                description="Implementation",
            )
        )
        tool = TaskDispatchTool(
            store=store,
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "worker" in desc
        assert "Implementation" in desc

    def test_description_no_subagents_available(self) -> None:
        tool = TaskDispatchTool(
            store=CommunicationTargetStore(),
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        assert "No subagents currently available" in desc
