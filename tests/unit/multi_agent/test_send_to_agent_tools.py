"""Tests for SendToAgentTool."""

from __future__ import annotations

import pytest

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)


class _RecordingService:
    def __init__(self) -> None:
        self.async_invocation_id: str | None = None
        self.last_target: CommunicationTarget | None = None

    async def send_async(
        self,
        *,
        target: CommunicationTarget,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        _ = content, context
        self.async_invocation_id = invocation_id
        self.last_target = target
        return "ok"


def _context() -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=object(),  # type: ignore[arg-type]
        tool_manager=object(),  # type: ignore[arg-type]
        session=SessionInfo.from_str("test.agent"),
    )


def _store_with_target() -> CommunicationTargetStore:
    """Pre-populated store for tests that need a valid target."""
    store = CommunicationTargetStore()
    store.add(
        CommunicationTarget(
            name="office-expert",
            kind=AgentCommKind.SUBAGENT,
        )
    )
    return store


class TestSendToAgentToolNames:
    def test_old_tool_names_are_absent(self) -> None:
        """Old tools must not be importable from tools module."""
        import modex_agent.multi_agent.tools as t

        assert not hasattr(t, "DispatchTaskTool"), "DispatchTaskTool should be removed"
        assert not hasattr(t, "SendMessageTool"), "SendMessageTool should be removed"
        assert not hasattr(t, "SendMessageAsyncTool"), "SendMessageAsyncTool should be removed"


class TestNewToolExports:
    def test_send_to_agent_tool_importable(self) -> None:
        from modex_agent.multi_agent.tools import SendToAgentTool

        assert SendToAgentTool.__name__ == "SendToAgentTool"

    def test_new_tools_exported_from_multi_agent(self) -> None:
        from modex_agent.multi_agent import SendToAgentTool

        assert SendToAgentTool is not None


class TestSchema:
    def test_normal_tool_has_invocation_id_param(self) -> None:
        """NORMAL agent tool: invocation_id is required for session management."""
        store = CommunicationTargetStore()  # for_subagent=False
        store.add(CommunicationTarget(name="office-expert", kind=AgentCommKind.SUBAGENT))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "invocation_id" in tool.parameters["properties"]
        assert "invocation_id" in tool.parameters["required"]

    def test_subagent_tool_has_no_invocation_id_param(self) -> None:
        """Subagent tool: no invocation_id — parent comm doesn't need sessions."""
        store = CommunicationTargetStore(for_subagent=True)
        store.add(CommunicationTarget(name="main", kind=AgentCommKind.NORMAL))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="worker"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "invocation_id" not in tool.parameters["properties"]
        assert "invocation_id" not in tool.parameters["required"]


class TestToolInvocationIdForwarding:
    @pytest.mark.asyncio
    async def test_tool_forwards_invocation_id_to_service(self) -> None:
        service = _RecordingService()
        tool = SendToAgentTool(
            store=_store_with_target(),
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


class TestToolInvocationIdNullStringNormalization:
    """LLMs may pass invocation_id as literal "null" / "Null" / "NULL" string.

    These must be treated as None (no invocation_id), matching the
    behavior when JSON null (Python None) is passed.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("null_value", ["null", "Null", "NULL", "nUlL"])
    async def test_string_null_treated_as_none(self, null_value: str) -> None:
        service = _RecordingService()
        tool = SendToAgentTool(
            store=_store_with_target(),
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
                invocation_id=null_value,  # string "null"
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        # "null" string must be treated the same as JSON null → forwarded as None
        assert service.async_invocation_id is None, (
            f"String {null_value!r} should be normalized to None, "
            f"got {service.async_invocation_id!r}"
        )

    @pytest.mark.asyncio
    async def test_none_python_null_still_works(self) -> None:
        """Python None (from JSON null) must still work as before."""
        service = _RecordingService()
        tool = SendToAgentTool(
            store=_store_with_target(),
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
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)

        assert result == "ok"
        assert service.async_invocation_id is None


class TestSendToAgentToolTargetValidation:
    @pytest.mark.asyncio
    async def test_rejects_unknown_target(self) -> None:
        service = _RecordingService()
        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="office-expert",
                kind=AgentCommKind.SUBAGENT,
            )
        )
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="nonexistent",
                content="test",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)
        assert "Error" in result
        assert "nonexistent" in result

    @pytest.mark.asyncio
    async def test_accepts_known_target(self) -> None:
        service = _RecordingService()
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
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
                content="test",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)
        assert result == "ok"


class TestSelfSendGuard:
    """Self-send (target_agent == caller's own agent_name) must be rejected
    with a message that names the caller, so an agent that does not know its
    own identity understands *why* the target it picked is itself."""

    @pytest.mark.asyncio
    async def test_self_send_rejected_with_caller_name(self) -> None:
        service = _RecordingService()
        tool = SendToAgentTool(
            store=_store_with_target(),
            source=AgentAddress(name="agent"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="agent",
                content="hello self",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)

        assert result.startswith("Error: You are 'agent'")
        assert "cannot send a message to yourself" in result
        assert service.last_target is None

    @pytest.mark.asyncio
    async def test_self_send_checked_before_target_lookup(self) -> None:
        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="agent",
                kind=AgentCommKind.NORMAL,
            )
        )
        service = _RecordingService()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="agent"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=service,  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_context())
        try:
            result = await tool.execute(
                target_agent="agent",
                content="hello self",
                invocation_id=None,
            )
        finally:
            current_agent_context.reset(token)

        assert "cannot send a message to yourself" in result
        assert service.last_target is None


class TestToolManagerIntegration:
    """ToolManager.get_tool_descriptions() MUST use get_dynamic_schema().

    The bug: _get_tool_schema() checks isinstance(tool, DynamicSchemaProvider).
    Tool base class does not inherit from DynamicSchemaProvider, so every tool
    falls through to get_schema() and returns the static constructor description.
    """

    def test_tool_manager_descriptions_use_dynamic_schema(self) -> None:
        from modex_agent.core.tool_manager import InMemoryToolManager

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
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )

        tm = InMemoryToolManager()
        tm.register(tool)
        schemas = tm.get_tool_descriptions()
        assert len(schemas) == 1
        desc = schemas[0]["function"]["description"]

        # Must contain dynamic target info, NOT the static fallback
        assert "scout" in desc, f"expected 'scout' in description, got: {desc}"
        assert "Fast recon" in desc, f"expected 'Fast recon' in description, got: {desc}"
        assert "worker" in desc, f"expected 'worker' in description, got: {desc}"
        assert "Communicate with another agent" in desc  # base instruction preserved


class TestSendToAgentToolDescription:
    """tool.description is the single source of truth — dynamically updated
    by the store on add_target / pop_target_by_name. No external refresh needed."""

    def test_description_contains_targets(self) -> None:
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "office-expert" in tool.description

    def test_description_updates_after_add_target(self) -> None:
        store = CommunicationTargetStore()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "No targets" in tool.description

        tool.add_target(
            CommunicationTarget(
                name="scout",
                kind=AgentCommKind.SUBAGENT,
                description="Fast recon",
            )
        )
        assert "scout" in tool.description
        assert "Fast recon" in tool.description
        assert "No targets" not in tool.description

    def test_description_updates_after_pop_target(self) -> None:
        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="scout",
                kind=AgentCommKind.SUBAGENT,
                description="Recon",
            )
        )
        store.add(
            CommunicationTarget(
                name="worker",
                kind=AgentCommKind.SUBAGENT,
                description="Impl",
            )
        )
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "scout" in tool.description
        assert "worker" in tool.description

        tool.pop_target_by_name("scout")
        assert "scout" not in tool.description
        assert "worker" in tool.description

    def test_duplicate_add_raises_value_error(self) -> None:
        """Duplicate target name must surface ValueError through add_target too."""
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="office-expert"):
            tool.add_target(
                CommunicationTarget(
                    name="office-expert",
                    kind=AgentCommKind.SUBAGENT,
                )
            )

    def test_pop_nonexistent_does_not_change_description(self) -> None:
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        before = tool.description
        tool.pop_target_by_name("nonexistent")
        assert tool.description is before

    def test_multiple_adds_and_pops(self) -> None:
        store = CommunicationTargetStore()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert "No targets" in tool.description

        # add agents one at a time
        tool.add_target(CommunicationTarget(name="alpha", kind=AgentCommKind.SUBAGENT))
        assert "alpha" in tool.description
        assert "beta" not in tool.description

        tool.add_target(CommunicationTarget(name="beta", kind=AgentCommKind.SUBAGENT))
        assert "alpha" in tool.description and "beta" in tool.description

        # pop alpha → only beta
        tool.pop_target_by_name("alpha")
        assert "alpha" not in tool.description
        assert "beta" in tool.description

        # pop beta → empty
        tool.pop_target_by_name("beta")
        assert "No targets" in tool.description

    def test_list_targets_returns_copy(self) -> None:
        """External code cannot mutate the internal target list."""
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        targets = tool.list_targets()
        targets.clear()
        assert tool.has_target("office-expert")  # internal list untouched

    def test_description_via_tool_manager(self) -> None:
        """ToolManager.get_tool_descriptions() returns dynamic description."""
        from modex_agent.core.tool_manager import InMemoryToolManager

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
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        tm = InMemoryToolManager()
        tm.register(tool)
        schemas = tm.get_tool_descriptions()
        desc = schemas[0]["function"]["description"]
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "worker" in desc
        assert "Implementation" in desc

    def test_subagent_description_truncated_in_send_to_agent(self) -> None:
        """Long subagent descriptions are truncated to ~40 chars with ``...``
        in the send_to_agent description, while normal target descriptions
        are kept in full."""
        store = CommunicationTargetStore()
        store.add(
            CommunicationTarget(
                name="coder",
                kind=AgentCommKind.SUBAGENT,
                description="Executes delegated implementation tasks with code generation and testing",
            )
        )
        store.add(
            CommunicationTarget(
                name="team-alpha",
                kind=AgentCommKind.NORMAL,
                description="Another team agent for cross-team work and coordination",
            )
        )
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        # Subagent description truncated — full text must NOT appear.
        assert "coder" in desc
        assert "code generation and testing" not in desc
        assert "Executes delegated implementation tasks..." in desc
        # Normal target description kept in full.
        assert "team-alpha" in desc
        assert "cross-team work and coordination" in desc


class TestSendToAgentToolDynamicSchema:
    def test_schema_name_and_parameters_intact(self) -> None:
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        assert schema["function"]["name"] == "send_to_agent"
        assert "target_agent" in schema["function"]["parameters"]["properties"]
        assert "invocation_id" in schema["function"]["parameters"]["properties"]

    def test_target_agent_has_enum_of_available_targets(self) -> None:
        """Dynamic schema must constrain target_agent to the exact list of available agents."""
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        store.add(CommunicationTarget(name="worker", kind=AgentCommKind.SUBAGENT))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        target_schema = schema["function"]["parameters"]["properties"]["target_agent"]
        assert target_schema.get("enum") == ["scout", "worker"]

    def test_target_agent_enum_updates_with_targets(self) -> None:
        """Adding/removing targets updates the enum in the dynamic schema."""
        store = CommunicationTargetStore()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        assert (
            "enum"
            not in tool.get_dynamic_schema()["function"]["parameters"]["properties"]["target_agent"]
        )

        tool.add_target(CommunicationTarget(name="alpha", kind=AgentCommKind.SUBAGENT))
        enum = tool.get_dynamic_schema()["function"]["parameters"]["properties"][
            "target_agent"
        ].get("enum")
        assert enum == ["alpha"]

        tool.add_target(CommunicationTarget(name="beta", kind=AgentCommKind.SUBAGENT))
        enum = tool.get_dynamic_schema()["function"]["parameters"]["properties"][
            "target_agent"
        ].get("enum")
        assert enum == ["alpha", "beta"]

    def test_target_agent_description_emphasizes_exact_name(self) -> None:
        """target_agent description must tell the LLM to use an exact listed name."""
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        desc = schema["function"]["parameters"]["properties"]["target_agent"]["description"]
        assert "exact name" in desc.lower()
        assert "available targets" in desc.lower()

    def test_invocation_id_description_mentions_returned_id(self) -> None:
        """invocation_id description must mention the returned id and continuation semantics."""
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        schema = tool.get_dynamic_schema()
        desc = schema["function"]["parameters"]["properties"]["invocation_id"]["description"]
        assert "tool result" in desc.lower()
        assert "invocation_id" in desc.lower()
        assert "follow-up" in desc.lower() or "continue" in desc.lower()
        assert "{invocation_id}.{target_agent}" in desc

    def test_description_documents_continuation_and_peer_guidance(self) -> None:
        """The tool description must document continuation (invocation_id)
        and peer messaging guidance so the LLM picks the right relationship.

        The static parameter schema stays kind-agnostic; the per-kind guidance
        lives in the dynamic tool description.
        """
        store = CommunicationTargetStore()
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        store.add(CommunicationTarget(name="coding_main", kind=AgentCommKind.NORMAL))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description.lower()
        # Continuation guidance — invocation_id mentioned for subagent sessions.
        assert "pass invocation_id" in desc
        # Peer messaging guidance — normal targets as equals.
        assert "peer agent" in desc
        assert "as an equal" in desc
        # Pointer to the `task` tool for new subagent dispatch.
        assert "`task` tool" in desc

    def test_static_parameters_not_mutated_by_dynamic_schema(self) -> None:
        """get_dynamic_schema() must not modify the shared parameter template."""
        store = _store_with_target()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        before = tool.parameters["properties"]["target_agent"]
        tool.get_dynamic_schema()
        after = tool.parameters["properties"]["target_agent"]
        assert before is after
        assert "enum" not in before


def _subagent_store() -> CommunicationTargetStore:
    return CommunicationTargetStore(for_subagent=True)


def _subagent_context(parent_name: str = "main") -> AgentContext:
    """A subagent AgentContext whose parent_session_id points at parent_name."""
    return AgentContext(
        system_prompt="",
        history=object(),  # type: ignore[arg-type]
        tool_manager=object(),  # type: ignore[arg-type]
        session=SessionInfo(
            session_id=f"conv-1.worker",
            agent_name="worker",
            parent_session_id=f"conv-1.{parent_name}",
        ),
    )


class TestSubagentDescriptionContent:
    """The subagent tool description must reflect what the subagent actually
    receives (an <agent_message source=...>) and steer it to consultation,
    not result-return. The main-agent description must NOT carry this text."""

    def test_subagent_description_contains_agent_message_and_source(self) -> None:
        store = _subagent_store()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="worker"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_subagent_context(parent_name="main"))
        try:
            desc = tool.description
        finally:
            current_agent_context.reset(token)
        assert "system-reminder" in desc
        assert "Message from agent" in desc
        assert "'main'" in desc  # the resolved parent echoed back

    def test_subagent_description_mentions_output_md_and_consultation(self) -> None:
        store = _subagent_store()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="worker"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        token = current_agent_context.set(_subagent_context())
        try:
            desc = tool.description
        finally:
            current_agent_context.reset(token)
        assert "OUTPUT.md" in desc
        assert "consultation" in desc.lower()

    def test_main_description_does_not_carry_subagent_text(self) -> None:
        """The main-agent description must not contain the subagent-only
        consultation/agent_message wording."""
        store = CommunicationTargetStore()  # normal mode
        store.add(CommunicationTarget(name="scout", kind=AgentCommKind.SUBAGENT))
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="main"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        desc = tool.description
        assert "<agent_message" not in desc
        assert "OUTPUT.md" not in desc
        assert "consultation" not in desc.lower()

    def test_subagent_description_no_parent_available(self) -> None:
        store = _subagent_store()
        tool = SendToAgentTool(
            store=store,
            source=AgentAddress(name="worker"),
            broker=object(),  # type: ignore[arg-type]
            registry=object(),  # type: ignore[arg-type]
            agent_bus=object(),  # type: ignore[arg-type]
            service=_RecordingService(),  # type: ignore[arg-type]
        )
        # No contextvar set → no resolvable parent.
        desc = tool.description
        assert "No parent" in desc
        assert "system-reminder" in desc  # description template still shown


class TestSubagentWiringSelectsSubagentMode:
    """The template wiring must register send_to_agent in subagent mode
    (for_subagent=True) — proven by the registered tool's parameters lacking
    invocation_id. Drives the real _register_send_to_agent wiring."""

    def test_wiring_produces_subagent_mode_tool(self) -> None:
        import dataclasses
        from unittest.mock import MagicMock

        from modex_agent.core.llm_struct import RuntimeSafetyPolicy
        from modex_agent.core.session_id import SessionIdFactory
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
        from modex_agent.multi_agent.template import AgentTemplate

        pool = MagicMock()
        pool.list_profiles.return_value = []
        deps = AgentMaterializeDeps(
            agent_factory=MagicMock(),
            pool=pool,
            session_factory=SessionIdFactory(),
            broker=MagicMock(),
            safety=RuntimeSafetyPolicy(),
            llm_model="gpt-4o",
            project_dir=None,
        )
        # Wire only what _register_send_to_agent reads.
        deps = dataclasses.replace(
            deps,
            agent_bus=MagicMock(),
            session_registry=None,
            workspace_path_resolver=None,
        )

        tm = InMemoryToolManager()
        AgentTemplate._register_send_to_agent(tm, deps, name="worker")

        descriptions = tm.get_tool_descriptions()
        send_tools = [d for d in descriptions if d["function"]["name"] == "send_to_agent"]
        assert len(send_tools) == 1
        params = send_tools[0]["function"]["parameters"]
        assert "invocation_id" not in params.get("properties", {})
        assert "invocation_id" not in params.get("required", [])


# -- Empty-store gating (pool_builder.create_pool skip condition) --


def test_empty_store_list_is_falsy():
    """An empty CommunicationTargetStore.list() is falsy.

    pool_builder.create_pool gates SendToAgentTool registration on
    ``if main_store.list():`` — an empty list (no subagents, no peers)
    must be falsy so the tool is not registered for solo agents.
    """
    store = CommunicationTargetStore()
    assert store.list() == []
    assert not store.list()


def test_nonempty_store_list_is_truthy():
    """A CommunicationTargetStore with at least one target is truthy."""
    store = CommunicationTargetStore()
    store.add(
        CommunicationTarget(
            name="explore",
            kind=AgentCommKind.SUBAGENT,
        )
    )
    assert store.list()
    assert len(store.list()) == 1
