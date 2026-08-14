from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.address import AgentAddress
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


def _task_tool(
    store: CommunicationTargetStore,
    service: _RecordingService | None = None,
) -> TaskDispatchTool:
    return TaskDispatchTool(
        store=store,
        source=AgentAddress(name="test"),
        service=service or _RecordingService(),  # type: ignore[arg-type]
    )


# -- 1. name -----------------------------------------------------------------


class TestTaskDispatchToolName:
    def test_task_tool_name_is_task(self) -> None:
        tool = _task_tool(CommunicationTargetStore())
        assert tool.name == "task"


# -- 2. params ---------------------------------------------------------------


class TestTaskDispatchToolParams:
    def test_params_have_target_agent_content_and_invocation_id(self) -> None:
        tool = _task_tool(CommunicationTargetStore())
        props = tool.parameters["properties"]
        assert "target_agent" in props
        assert "content" in props
        assert "invocation_id" in props
        required = tool.parameters["required"]
        assert "target_agent" in required
        assert "content" in required
        assert "invocation_id" not in required


# -- 3, 4, 12. dynamic schema ------------------------------------------------


class TestTaskDispatchToolDynamicSchema:
    def test_target_agent_enum_includes_only_subagents(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        tool = _task_tool(store)
        schema = tool.get_dynamic_schema()
        target_schema = schema["function"]["parameters"]["properties"]["target_agent"]
        assert target_schema.get("enum") == ["scout"]

    def test_dynamic_schema_not_mutating_static_params(self) -> None:
        store = _store_with_subagent_target()
        tool = _task_tool(store)
        before = tool.parameters["properties"]["target_agent"]
        tool.get_dynamic_schema()
        after = tool.parameters["properties"]["target_agent"]
        assert before is after
        assert "enum" not in before


class TestTaskDispatchToolTargetManagement:
    def test_add_target_and_has_target(self) -> None:
        tool = _task_tool(CommunicationTargetStore())
        target = CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT)

        tool.add_target(target)

        assert tool.has_target("scout")

    def test_pop_target_by_name(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        tool = _task_tool(store)

        tool.pop_target_by_name("scout")

        assert not tool.has_target("scout")


# -- 5, 6, 7, 13. execute ----------------------------------------------------


class TestTaskDispatchToolExecute:
    @pytest.mark.asyncio
    async def test_execute_calls_send_async_with_invocation_id_none(self) -> None:
        service = _RecordingService()
        tool = _task_tool(_store_with_subagent_target(), service)
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
        tool = _task_tool(_store_with_subagent_target(), service)
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
        assert "Available subagents:" in result
        assert "office-expert" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_execute_with_invocation_id_continues_session(self) -> None:
        service = _RecordingService()
        tool = _task_tool(_store_with_subagent_target(), service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="test",
                invocation_id="abc123",
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.async_invocation_id == "abc123"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("null_value", ["null", "Null", "NULL", "none", ""])
    async def test_execute_null_string_treated_as_new_task(self, null_value: str) -> None:
        service = _RecordingService()
        tool = _task_tool(_store_with_subagent_target(), service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="office-expert",
                content="test",
                invocation_id=null_value,
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.async_invocation_id is None, (
            f"String {null_value!r} should be normalized to None, "
            f"got {service.async_invocation_id!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_rejects_peer_target(self) -> None:
        service = _RecordingService()
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        tool = _task_tool(store, service)
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="peer-main",
                content="test",
            )
        finally:
            current_agent_context.reset(token)

        assert "Error" in result
        assert "peer-main" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_self_dispatch_rejected(self) -> None:
        service = _RecordingService()
        tool = _task_tool(_store_with_subagent_target(), service)
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
        tool = _task_tool(_store_with_subagent_target())
        desc = tool.description
        for keyword in ("TASK", "CONTEXT", "SCOPE", "OUTPUT", "VERIFICATION", "BOUNDARIES"):
            assert keyword in desc, f"expected {keyword!r} in description"

    def test_description_contains_when_not_to_use(self) -> None:
        tool = _task_tool(_store_with_subagent_target())
        desc = tool.description
        assert "When NOT to use" in desc
        lowered = desc.lower()
        assert "read" in lowered or "grep" in lowered or "glob" in lowered

    def test_description_contains_concurrency_guidance(self) -> None:
        tool = _task_tool(_store_with_subagent_target())
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
        tool = _task_tool(store)
        desc = tool.description
        assert "## Subagents" in desc
        assert "Available subagents:" in desc
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "worker" in desc
        assert "Implementation" in desc

    def test_description_no_subagents_available(self) -> None:
        tool = _task_tool(CommunicationTargetStore())
        desc = tool.description
        assert "No subagents currently available" in desc

    def test_description_ignores_peer_targets(self) -> None:
        store = _store_with_subagent_target()
        store.add(
            CommunicationTarget(
                name="peer-main",
                kind=AgentCommKind.NORMAL,
                description="Planning partner",
            )
        )
        desc = _task_tool(store).description

        assert "## Peer Agents" not in desc
        assert "peer-main" not in desc
        assert "Planning partner" not in desc
        assert "## Subagents" in desc

    def test_description_no_peer_section_when_only_subagents(self) -> None:
        desc = _task_tool(_store_with_subagent_target()).description

        assert "## Peer Agents" not in desc

    def test_description_only_peers_reports_no_subagents(self) -> None:
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        desc = _task_tool(store).description

        assert "## Subagents" not in desc
        assert "No subagents currently available" in desc

    def test_description_no_forbidden_words(self) -> None:
        store = _store_with_subagent_target()
        store.add(CommunicationTarget(name="peer-main", kind=AgentCommKind.NORMAL))
        desc = _task_tool(store).description
        lowered = desc.lower()

        assert "pool" not in lowered
        assert "system-reminder" not in lowered
        assert "normal agent" not in lowered
        assert "pass null" not in lowered
