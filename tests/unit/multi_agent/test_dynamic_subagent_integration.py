"""Integration test: template → registry → system prompt resolution → XML messages."""

import tempfile
from unittest.mock import AsyncMock, MagicMock

from modex_agent.core import AgentCommKind
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import StopReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.message_format import (
    ResultMeta,
    ResultStatus,
    SourceLabel,
    build_agent_comm_message,
)
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import CommunicationTarget


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree



def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


def test_xml_message_round_trip():
    """Verify unified markdown formats are self-describing and parseable."""
    # Agent sends a message
    msg = build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source="office-expert",
        content="PDF 转换完成，共 12 页。",
        invocation_id="abc123",
    )
    assert "Message from agent" in msg
    assert "Message from agent 'office-expert'" in msg
    assert "PDF 转换完成" in msg

    # Hook generates a result
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="office-expert",
        content="任务完成。文件路径：/output/result.docx",
        invocation_id="abc123",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.MISSED_COMMUNICATION,
        ),
    )
    assert "Message from subagent" in result
    assert "status: success" in result
    assert "任务完成" in result


def _make_mock_pool():
    """Create a mock AgentPool that supports register_resident (async) + get (sync)."""
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=MagicMock(pipeline=MagicMock()))
    return pool


class TestDynamicCreationAgentAddressBug:
    """ADR-0015 D3: _create_dynamic_subagent folded into AgentTemplate.materialize;
    see test_template_materialize.py for the comm_kind-inference behavior."""


class TestInvocationIdNullCreatesNewSubagent:
    """invocation_id=null should work for template targets (auto-create new subagent).

    The LLM should not need to know about NORMAL/SUBAGENT.
    null = new task, concrete value = continue existing session.
    """

    # ADR-0015 D3: test_null_invocation_id_creates_template_subagent deleted —
    # cold-start materialize is now covered by
    # test_drainer_materializes_missing_subagent_on_first_drain
    # (tests/unit/multi_agent/test_drainer_protocol.py).

    async def test_null_invocation_id_normal_agent(self):
        """send_to_agent(target='normal-agent', invocation_id=null) sends normally."""
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.communication import AgentCommunicationService

        mock_registry = MagicMock()
        mock_registry.get_descriptor.return_value = None
        mock_profile = MagicMock()
        mock_profile.comm_kind = AgentCommKind.NORMAL
        mock_registry.get_profile.return_value = mock_profile
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            registry=mock_registry,
            tree=MagicMock(spec=SessionTreeManager),
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
        )

        result = await service.send_async(
            target=_tgt("other-agent", AgentCommKind.SUBAGENT),
            content="Hello",
            invocation_id=None,
            context=ctx,
        )

        assert "Error" not in result

    async def test_concrete_invocation_id_continues_session(self):
        """send_to_agent(target='helper', invocation_id='abc123') continues existing session."""
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.communication import AgentCommunicationService

        mock_registry = MagicMock()
        mock_descriptor = MagicMock()
        mock_descriptor.comm_kind = AgentCommKind.SUBAGENT
        mock_registry.get_descriptor.return_value = mock_descriptor
        mock_registry.get_profile.return_value = None
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            registry=mock_registry,
            tree=MagicMock(spec=SessionTreeManager),
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
        )

        result = await service.send_async(
            target=_tgt("helper", AgentCommKind.SUBAGENT),
            content="Continue task",
            invocation_id="abc12345",
            context=ctx,
        )

        assert "Error" not in result


class TestInvocationIdDescriptionHidesCommKind:
    """The invocation_id parameter description points to the notification/consultation source; it must not mention the NORMAL kind."""

    def test_param_description_points_to_notification_source(self):
        from modex_agent.multi_agent.tools import _NORMAL_PARAMS

        desc = _NORMAL_PARAMS["properties"]["invocation_id"]["description"].lower()
        assert "notification or" in desc
        assert "consultation message" in desc
        assert "normal" not in desc

    def test_tool_description_no_kind_mention(self):
        from modex_agent.multi_agent.tools import CommunicationTargetStore, SendToAgentTool

        store = CommunicationTargetStore()
        tool = SendToAgentTool(
            store=store,
            source=MagicMock(),
            service=MagicMock(),
        )

        desc = tool.description.lower()
        assert "normal" not in desc
        assert "subagent" not in desc


class TestSubagentIdentityResolution:
    """Bug: communication tools hardcode source="main", causing subagents to
    use wrong identity. When a subagent calls list_communication_targets, it
    must filter out SUBAGENT targets (only see NORMAL). When it sends, envelope
    source must be the subagent name, not "main".
    """

    async def test_subagent_send_has_correct_source(self):
        """When subagent sends via send_to_agent, envelope source must be subagent name."""
        from modex_agent.core.agent import current_agent_context
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.communication import AgentCommunicationService

        sent_envelopes: list = []

        capture_tree = MagicMock(spec=SessionTreeManager)

        async def _capture_deliver(sid: str, env: object) -> None:
            sent_envelopes.append(env)

        capture_tree.deliver = _capture_deliver

        mock_registry = MagicMock()
        mock_descriptor = MagicMock()
        mock_descriptor.comm_kind = AgentCommKind.NORMAL
        mock_registry.get_descriptor.return_value = mock_descriptor
        mock_registry.get_profile.return_value = None
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),  # hardcoded at construction
            registry=mock_registry,
            tree=capture_tree,
        )

        # Subagent context
        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("conv-1.helper"),
            comm_kind=AgentCommKind.SUBAGENT,
        )
        token = current_agent_context.set(ctx)
        try:
            result = await service.send_async(
                target=_tgt("main", AgentCommKind.NORMAL),
                content="Task done",
                invocation_id=None,
                context=ctx,
            )

            assert "Error" not in result
            # The envelope source must be the subagent, not "main"
            assert len(sent_envelopes) == 1
            env = sent_envelopes[0]
            assert env.source.name != "main", "Subagent's envelope source must NOT be 'main'"
        finally:
            current_agent_context.reset(token)


class TestSubagentIsolation:
    """ADR-0015 D3: _create_dynamic_subagent folded into AgentTemplate.materialize;
    dedicated-context-manager and dedicated-tool-manager construction is now
    materialize's job (see test_template_materialize.py). The two original
    tests are deleted."""

    # ADR-0015 D3: test_subagent_gets_dedicated_context_manager deleted.
    # ADR-0015 D3: test_subagent_gets_dedicated_tool_manager deleted.


class TestSubagentMemoryCorrectness:
    """ADR-0015 D3: _create_dynamic_subagent folded into AgentTemplate.materialize;
    memory-system-context-manager construction is now materialize's job
    (see test_template_materialize.py)."""

    # ADR-0015 D3: test_subagent_gets_memory_system_context_manager deleted.


class TestAgentMessageXmlWrapping:
    """Outgoing agent messages must be wrapped in <agent_message> XML.

    Per spec Section 4.1: Framework fills source and invocation_id;
    LLM only provides content. The XML wrapping happens at send time.
    The receiving agent stores it in memory as-is via InboxFlushHook.
    """

    # ADR-0015 D3: test_task_request_wraps_content_in_agent_message_xml
    # deleted — it exercised the deleted _create_dynamic_subagent's send path.
    # XML wrapping on send is covered by test_agent_message_wraps_content_in_xml
    # below (drives the still-existing send_async path) and by the build_agent_message
    # round-trip test at module top (test_xml_message_round_trip).

    async def test_agent_message_wraps_content_in_xml(self):
        """Normal agent_message must also be XML-wrapped."""
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.communication import AgentCommunicationService
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        sent_payloads: list = []

        capture_tree = MagicMock(spec=SessionTreeManager)

        async def _capture_deliver(sid: str, env: object) -> None:
            sent_payloads.append(env.payload)  # type: ignore[attr-defined]

        capture_tree.deliver = _capture_deliver

        mock_registry = MagicMock()
        mock_registry.get_descriptor.return_value = AgentDescriptor(
            address=AgentAddress(name="helper"),
            comm_kind=AgentCommKind.SUBAGENT,
        )

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            registry=mock_registry,
            tree=capture_tree,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
        )

        result = await service.send_async(
            target=_tgt("helper", AgentCommKind.SUBAGENT),
            content="Follow-up question",
            invocation_id="existing123",
            context=ctx,
        )

        assert "Error" not in str(result)
        assert len(sent_payloads) >= 1
        payload = sent_payloads[0]
        content = payload.get("content", "")

        assert "Message from agent" in content, (
            f"Agent messages must be markdown-wrapped, got: {content[:100]}"
        )
        assert "invocation_id" not in content
        assert "Follow-up question" in content


class TestSessionRoutingSameAgentDifferentInvocation:
    """Same agent name + different invocation_ids → different sessions.

    Session isolation is driven by invocation_id via SessionIdFactory.
    Same external_id = same session (memory inheritance).
    Different external_id = different session (fresh context).
    """

    def test_same_agent_different_invocation_produces_different_sessions(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_a = factory.create(
            agent_name="query-12306",
            external_id="abc123",
        )
        sid_b = factory.create(
            agent_name="query-12306",
            external_id="def456",
        )

        # Different invocation_ids must produce different session IDs
        assert str(sid_a) != str(sid_b)
        assert sid_a.agent_name == "query-12306"
        assert sid_b.agent_name == "query-12306"

    def test_same_invocation_produces_same_session(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_1 = factory.create(
            agent_name="query-12306",
            external_id="abc123",
        )
        sid_2 = factory.create(
            agent_name="query-12306",
            external_id="abc123",
        )

        # Same external_id → same snowflake → same session (memory inheritance)
        assert str(sid_1) == str(sid_2)

    def test_different_agent_same_invocation_different_sessions(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_a = factory.create(
            agent_name="query-12306",
            external_id="abc123",
        )
        sid_b = factory.create(
            agent_name="office-expert",
            external_id="abc123",
        )

        # Different agent names → different sessions even with same external_id
        assert str(sid_a) != str(sid_b)

    # ADR-0015 D3: test_second_empty_invocation_id_does_not_recreate_agent
    # deleted — send_async is now a pure router; subagent materialization is
    # Drainer-driven (lazy on first drain), not synchronous in send_async.
    # The pure session-id routing behavior this class cares about is covered by
    # the three SessionIdFactory tests above.


class TestSubagentSafetyHooks:
    """ADR-0015 D3: _wire_subagent_hooks deleted from the service; safety hooks
    are now wired inside AgentTemplate.materialize."""

    # ADR-0015 D3: test_hooks_not_wired_without_pipeline deleted.


class TestOutputMdInjection:
    """Verify OUTPUT.md is no longer injected into subagent system prompts
    (deliverable is now reply-text-based). Path computation tests remain
    for the underlying directory structure."""

    def test_output_md_path_contains_session_structure(self):
        """OUTPUT.md path must contain session-id components and end with OUTPUT.md."""
        from pathlib import Path as _Path

        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()
        session = factory.create(
            agent_name="reviewer",
            external_id="abc123",
        )
        session_id = session.session_id
        runtime_dir = _Path(tempfile.gettempdir()) / "runtime_state" / "coding"
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"

        # Must be absolute (runtime_dir is absolute → output_path is absolute)
        assert output_path.is_absolute(), "OUTPUT.md path must be absolute"
        assert str(output_path).endswith("OUTPUT.md")
        assert ".reviewer" in str(output_path)
        assert "output" in str(output_path)

    def test_full_template_does_not_get_scoped_tools(self):
        """READ_WRITE template uses standard write/edit, not scoped versions."""
        from modex_agent.tools.presets import ToolPreset, get_preset_tools

        tools = get_preset_tools(ToolPreset.READ_WRITE)
        tool_names = {t.name for t in tools}
        assert "write" in tool_names
        assert "edit" in tool_names

    # ADR-0015 D3: test_system_prompt_includes_output_md_protocol deleted —
    # prompt assembly moved into AgentTemplate.materialize. OUTPUT.md is no
    # longer injected — covered by test_built_system_prompt_does_not_contain_output_md.
    # ADR-0015 D3: test_output_md_before_fork_context deleted — same reason;
    # OutputMdProvider is deprecated (T5), no longer registered in system.py.

    async def test_built_system_prompt_does_not_contain_output_md(self):
        """OutputMdProvider is deprecated (T5); built prompt must NOT contain
        OUTPUT.md or 'work is lost' wording."""
        import tempfile
        from pathlib import Path as _Path

        from modex_agent.ioc.configs.memory import MemoryConfig
        from modex_agent.ioc.factories.descriptors import build_session_only_memory
        from modex_agent.memory.scope import MemoryAgentRole

        runtime_dir = _Path(tempfile.mkdtemp()) / "runtime"
        session_id = "conv-1.reviewer.abc123"
        output_base_dir = runtime_dir / "output"

        system_prompt = "You are a code reviewer."

        workspace = _Path(tempfile.mkdtemp()) / "memory"
        ctx_mgr = build_session_only_memory(
            cfg=MemoryConfig(),
            workspace=workspace,
            agent_id="reviewer",
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=system_prompt,
            output_base_dir=output_base_dir,
        )

        # load() sets _last_session_id so providers get the right session
        await ctx_mgr.load(session_id)
        built = await ctx_mgr.build_system_prompt(tool_manager=None)

        assert "OUTPUT.md" not in built
        assert "work is lost" not in built


class TestSubagentToolInstanceIsolation:
    """ADR-0015 D3: _create_dynamic_subagent folded into AgentTemplate.materialize;
    the tool_manager is now built fresh inside each materialize() call, so
    distinct-by-construction holds. The three original tests are deleted."""

    # ADR-0015 D3: test_two_subagents_get_distinct_tool_managers deleted.
    # ADR-0015 D3: test_tool_instances_not_shared_between_subagents deleted.
    # ADR-0015 D3: test_subagents_have_independent_preset_tool_instances deleted.
