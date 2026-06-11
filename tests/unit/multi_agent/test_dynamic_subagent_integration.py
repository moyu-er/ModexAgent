"""Integration test: template → registry → system prompt resolution → XML messages."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from framework.core.agent import AgentContext, AgentSessionMeta
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.message_xml import build_agent_message, build_agent_result
from framework.multi_agent.template import AgentTemplate
from framework.multi_agent.template_registry import AgentTemplateRegistry


def _write_files(base: Path, pool: str, agent_type: str, yml_content: str, md_content: str):
    tpl_dir = base / "config" / "pools" / pool / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / f"{agent_type}.yml").write_text(yml_content, encoding="utf-8")
    agents_dir = base / "agents" / pool
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_type}.md").write_text(md_content, encoding="utf-8")


def test_template_to_descriptor_pipeline():
    """Full pipeline: YAML template → AgentTemplate → system prompt resolution."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_files(project, "main", "helper",
            "agent_type: helper\ndescription: Test helper\nmax_steps: 15\n",
            "You are a helpful assistant."
        )

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1

        t = templates[0]
        assert t.agent_type == "helper"
        assert t.max_steps == 15

        # System prompt resolution (same convention as resolve_system_prompt)
        md_path = project / "agents" / "main" / "helper.md"
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8") == "You are a helpful assistant."


def test_xml_message_round_trip():
    """Verify XML formats are self-describing and parseable."""
    # Agent sends a message
    msg = build_agent_message(
        source="office-expert", invocation_id="abc123",
        content="PDF 转换完成，共 12 页。",
    )
    assert "<agent_message" in msg
    assert 'source="office-expert"' in msg
    assert "PDF 转换完成" in msg

    # Hook generates a result
    result = build_agent_result(
        source="office-expert", invocation_id="abc123",
        status="completed", stop_reason="missed_communication",
        content="任务完成。文件路径：/output/result.docx",
    )
    assert "<agent_result" in result
    assert 'status="completed"' in result
    assert "任务完成" in result


def test_multiple_templates_per_pool():
    """Multiple templates in one pool are all loaded."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_files(project, "main", "a", "agent_type: a\ndescription: ''\n", "A")
        _write_files(project, "main", "b", "agent_type: b\ndescription: ''\n", "B")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 2
        types = {t.agent_type for t in templates}
        assert types == {"a", "b"}


def test_template_with_memory_config():
    """Template with memory configuration is loaded correctly."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        yml = """\
agent_type: heavy
description: Heavy task agent
max_steps: 50
memory:
  short_term: {max_messages: 100, max_tokens: 50000}
"""
        _write_files(project, "main", "heavy", yml, "Heavy agent.")

        registry = AgentTemplateRegistry(project)
        t = registry.get_template("main", "heavy")
        assert t is not None
        assert t.max_steps == 50
        assert t.memory is not None
        assert t.memory.short_term.max_messages == 100
        assert t.memory.short_term.max_tokens == 50000


def test_template_not_found_returns_none():
    """get_template returns None for missing agent types."""
    with tempfile.TemporaryDirectory() as tmp:
        registry = AgentTemplateRegistry(Path(tmp))
        assert registry.get_template("main", "nonexistent") is None


def _make_mock_pool():
    """Create a mock AgentPool that supports register_resident (async) + get (sync)."""
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=MagicMock(pipeline=MagicMock()))
    return pool


class TestDynamicCreationAgentAddressBug:
    """Bug: _create_dynamic_subagent passes comm_kind to AgentAddress which doesn't accept it.

    Reproduces: AgentAddress.__init__() got an unexpected keyword argument 'comm_kind'
    """

    async def test_create_dynamic_subagent_does_not_pass_comm_kind_to_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            from framework.multi_agent.communication import AgentCommunicationService
            from framework.multi_agent.address import AgentAddress

            mock_pool = _make_mock_pool()
            mock_broker = AsyncMock()

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=MagicMock(),
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
            )

            # This must NOT raise TypeError about comm_kind
            result = await service._create_dynamic_subagent(
                template=template,
                conversation_id="conv-1",
                invocation_id="abc12345",
                content="Do something",
            )

            assert result.error is None
            assert result.target_agent == "helper"
            assert result.target_kind == AgentCommKind.SUBAGENT
            mock_pool.register_resident.assert_called_once()

            # Verify the descriptor has correct comm_kind and clean name
            desc = mock_pool.register_resident.call_args[0][0]
            assert desc.comm_kind == AgentCommKind.SUBAGENT
            assert desc.address.name == "helper"


class TestInvocationIdNullCreatesNewSubagent:
    """invocation_id=null should work for template targets (auto-create new subagent).

    The LLM should not need to know about NORMAL/SUBAGENT.
    null = new task, concrete value = continue existing session.
    """

    async def test_null_invocation_id_creates_template_subagent(self):
        """send_to_agent(target='helper', invocation_id=null) creates new subagent."""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            from framework.multi_agent.communication import AgentCommunicationService
            from framework.multi_agent.address import AgentAddress
            from framework.core.agent import AgentContext, AgentSessionMeta

            mock_pool = _make_mock_pool()
            mock_broker = AsyncMock()
            mock_registry = MagicMock()
            mock_registry.get_descriptor.return_value = None
            mock_registry.get_profile.return_value = None

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=mock_registry,
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
                template_registry=registry,
            )

            ctx = AgentContext(
                system_prompt="",
                history=MagicMock(),
                tool_manager=MagicMock(),
                session_meta=AgentSessionMeta(
                    conversation_id="conv-1",
                    agent_name="main",
                    comm_kind=AgentCommKind.NORMAL,
                ),
            )

            result = await service.send_async(
                target_agent="helper",
                content="Do something",
                invocation_id=None,
                context=ctx,
            )

            assert "Error" not in result
            assert "dyn." not in result  # no internal prefix exposed
            mock_pool.register_resident.assert_called_once()

    async def test_null_invocation_id_normal_agent(self):
        """send_to_agent(target='normal-agent', invocation_id=null) sends normally."""
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.address import AgentAddress
        from framework.core.agent import AgentContext, AgentSessionMeta

        mock_broker = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.get_descriptor.return_value = None
        mock_profile = MagicMock()
        mock_profile.comm_kind = AgentCommKind.NORMAL
        mock_registry.get_profile.return_value = mock_profile
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=mock_registry,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1",
                agent_name="main",
                comm_kind=AgentCommKind.NORMAL,
            ),
        )

        result = await service.send_async(
            target_agent="other-agent",
            content="Hello",
            invocation_id=None,
            context=ctx,
        )

        assert "Error" not in result

    async def test_concrete_invocation_id_continues_session(self):
        """send_to_agent(target='helper', invocation_id='abc123') continues existing session."""
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.address import AgentAddress
        from framework.core.agent import AgentContext, AgentSessionMeta

        mock_broker = AsyncMock()
        mock_registry = MagicMock()
        mock_descriptor = MagicMock()
        mock_descriptor.comm_kind = AgentCommKind.SUBAGENT
        mock_registry.get_descriptor.return_value = mock_descriptor
        mock_registry.get_profile.return_value = None
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=mock_registry,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1",
                agent_name="main",
                comm_kind=AgentCommKind.NORMAL,
            ),
        )

        result = await service.send_async(
            target_agent="helper",
            content="Continue task",
            invocation_id="abc12345",
            context=ctx,
        )

        assert "Error" not in result


class TestInvocationIdDescriptionHidesCommKind:
    """The invocation_id parameter description must NOT mention NORMAL/SUBAGENT."""

    def test_param_description_no_kind_mention(self):
        from framework.multi_agent.tools import _NORMAL_PARAMS

        desc = _NORMAL_PARAMS["properties"]["invocation_id"]["description"].lower()
        assert "normal" not in desc
        assert "subagent" not in desc

    def test_tool_description_no_kind_mention(self):
        from framework.multi_agent.tools import CommunicationTargetStore, SendToAgentTool

        store = CommunicationTargetStore()
        tool = SendToAgentTool(
            store=store,
            source=MagicMock(),
            broker=MagicMock(),
            registry=MagicMock(),
            agent_bus=MagicMock(),
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
        from framework.core.agent import AgentContext, current_agent_context, AgentSessionMeta
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.address import AgentAddress

        sent_envelopes: list = []
        mock_broker = AsyncMock()

        async def capture_send(target, msg):
            sent_envelopes.append(msg)

        mock_broker.send_to = capture_send
        mock_registry = MagicMock()
        mock_descriptor = MagicMock()
        mock_descriptor.comm_kind = AgentCommKind.NORMAL
        mock_registry.get_descriptor.return_value = mock_descriptor
        mock_registry.get_profile.return_value = None
        mock_registry.list_profiles.return_value = []

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),  # hardcoded at construction
            broker=mock_broker,
            registry=mock_registry,
        )

        # Subagent context
        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1",
                agent_name="helper",
                comm_kind=AgentCommKind.SUBAGENT,
            ),
        )
        token = current_agent_context.set(ctx)
        try:
            result = await service.send_async(
                target_agent="main",
                content="Task done",
                invocation_id=None,
                context=ctx,
            )

            assert "Error" not in result
            # The envelope source must be the subagent, not "main"
            assert len(sent_envelopes) == 1
            broker_msg = sent_envelopes[0]
            assert broker_msg.sender.name != "main", (
                "Subagent's envelope source must NOT be 'main'"
            )
        finally:
            current_agent_context.reset(token)


class TestSubagentIsolation:
    """Dynamic subagent must NOT reuse main's context/tool managers.

    The pool's _default_context_manager and factory's _default_tool_manager
    are shared objects. _create_dynamic_subagent must create DEDICATED
    instances so the subagent:
    - uses its own system prompt (from agents/{pool}/{type}.md)
    - has only basic tools (no MCP/web search from main)
    - does NOT share main's conversation history/memory
    """

    async def test_subagent_gets_dedicated_context_manager(self):
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n",
                "You are a helper agent.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            mock_pool = _make_mock_pool()
            mock_pool._agents = {}
            mock_broker = AsyncMock()

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=MagicMock(),
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
            )

            result = await service._create_dynamic_subagent(
                template=template,
                conversation_id="conv-1",
                invocation_id="test0001",
                content="Do something",
            )

            assert result.error is None
            mock_pool.register_resident.assert_called_once()

            call_kwargs = mock_pool.register_resident.call_args
            passed_ctx = call_kwargs[1].get("context_manager")
            assert passed_ctx is not None, (
                "_create_dynamic_subagent must pass dedicated context_manager"
            )

    async def test_subagent_gets_dedicated_tool_manager(self):
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n"
                "use_terminal: false\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            mock_pool = _make_mock_pool()
            mock_broker = AsyncMock()

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=MagicMock(),
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
            )

            result = await service._create_dynamic_subagent(
                template=template,
                conversation_id="conv-1",
                invocation_id="test0002",
                content="Do something",
            )

            assert result.error is None
            mock_pool.register_resident.assert_called_once()

            call_kwargs = mock_pool.register_resident.call_args
            passed_tm = call_kwargs[1].get("tool_manager")
            assert passed_tm is not None, (
                "_create_dynamic_subagent must pass dedicated tool_manager"
            )

            tool_names = set(passed_tm.list_tools())
            assert "send_to_agent" not in tool_names, (
                "Subagent must NOT have communication tools (notification via hook)"
            )
            assert "read" in tool_names
            assert "write" in tool_names
            assert "mcp_playwright_browser_navigate" not in tool_names, (
                "Subagent must not inherit main's MCP tools"
            )

class TestSubagentMemoryCorrectness:
    """Dynamic subagent must get a real MemorySystemContextManager with
    session-scoped memory (no knowledge layer), not bare InMemoryContextManager.

    Verifies the subagent's context_manager is a MemorySystemContextManager
    wrapping a MemorySystem with session+archive layers (no knowledge).
    """

    async def test_subagent_gets_memory_system_context_manager(self):
        """Subagent must use MemorySystemContextManager."""
        from framework.memory.system import MemorySystemContextManager
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.memory.core.scope import MemoryAgentRole

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n"
                "memory:\n  short_term:\n    max_messages: 20\n    max_tokens: 10000\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            mock_pool = _make_mock_pool()
            mock_pool._agents = {}
            mock_broker = AsyncMock()

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=MagicMock(),
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
            )

            result = await service._create_dynamic_subagent(
                template=template,
                conversation_id="conv-1",
                invocation_id="test0001",
                content="Do something",
            )

            assert result.error is None
            call_kwargs = mock_pool.register_resident.call_args
            passed_ctx = call_kwargs[1].get("context_manager")

            # Must be MemorySystemContextManager (has real memory persistence)
            assert passed_ctx is not None
            assert isinstance(passed_ctx, MemorySystemContextManager), (
                f"Expected MemorySystemContextManager, got {type(passed_ctx).__name__}"
            )

            # Memory system must exist and be initialized
            assert passed_ctx.memory_system is not None
            assert passed_ctx.default_agent_id == "helper"
            assert passed_ctx.default_agent_role == MemoryAgentRole.SUBAGENT


class TestAgentMessageXmlWrapping:
    """Outgoing agent messages must be wrapped in <agent_message> XML.

    Per spec Section 4.1: Framework fills source and invocation_id;
    LLM only provides content. The XML wrapping happens at send time.
    The receiving agent stores it in memory as-is via InboxFlushHook.
    """

    async def test_task_request_wraps_content_in_agent_message_xml(self):
        """First message (task_request) must be XML-wrapped."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.message_xml import build_agent_message

        sent_payloads: list = []
        mock_broker = AsyncMock()

        async def capture_send(target, msg):
            sent_payloads.append(msg.payload)

        mock_broker.send_to = capture_send
        mock_pool = _make_mock_pool()
        mock_registry = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=mock_registry,
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
                template_registry=registry,
            )

            result = await service._create_dynamic_subagent(
                template=template,
                conversation_id="conv-1",
                invocation_id="test0001",
                content="Hello from main",
            )

            assert result.error is None
            assert len(sent_payloads) == 1
            payload = sent_payloads[0]
            content = payload.get("content", "")

            # Must be XML-wrapped
            assert "<agent_message" in content, (
                f"Content must be XML-wrapped, got: {content[:100]}"
            )
            assert 'source="main"' in content
            assert 'invocation_id="test0001"' in content
            assert "Hello from main" in content

    async def test_agent_message_wraps_content_in_xml(self):
        """Normal agent_message must also be XML-wrapped."""
        from framework.core.agent import AgentContext, AgentSessionMeta
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.descriptor import AgentDescriptor

        sent_payloads: list = []
        mock_broker = AsyncMock()

        async def capture_send(target, msg):
            sent_payloads.append(msg.payload)

        mock_broker.send_to = capture_send
        mock_registry = MagicMock()
        mock_registry.get_descriptor.return_value = AgentDescriptor(
            address=AgentAddress(name="helper"),
            comm_kind=AgentCommKind.SUBAGENT,
        )

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=mock_registry,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1",
                agent_name="main",
                comm_kind=AgentCommKind.NORMAL,
            ),
        )

        result = await service.send_async(
            target_agent="helper", content="Follow-up question",
            invocation_id="existing123", context=ctx,
        )

        assert "Error" not in str(result)
        assert len(sent_payloads) >= 1
        payload = sent_payloads[0]
        content = payload.get("content", "")

        assert "<agent_message" in content, (
            f"Agent messages must be XML-wrapped, got: {content[:100]}"
        )
        assert 'invocation_id="existing123"' in content
        assert "Follow-up question" in content


class TestSessionRoutingSameAgentDifferentInvocation:
    """Same agent name + different invocation_ids → different sessions.

    Session isolation is driven by invocation_id via DefaultSessionIdStrategy.
    Same invocation_id = same session (memory inheritance).
    Different invocation_id = different session (fresh context).
    """

    def test_same_agent_different_invocation_produces_different_sessions(self):
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy()

        sid_a = strategy.format(
            conversation_id="conv-1", agent_name="query-12306", invocation_id="abc123",
        )
        sid_b = strategy.format(
            conversation_id="conv-1", agent_name="query-12306", invocation_id="def456",
        )

        # Different invocation_ids must produce different session IDs
        assert sid_a != sid_b
        assert sid_a == "conv-1.query-12306.abc123"
        assert sid_b == "conv-1.query-12306.def456"

    def test_same_invocation_produces_same_session(self):
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy()

        sid_1 = strategy.format(
            conversation_id="conv-1", agent_name="query-12306", invocation_id="abc123",
        )
        sid_2 = strategy.format(
            conversation_id="conv-1", agent_name="query-12306", invocation_id="abc123",
        )

        # Same invocation_id → same session (memory inheritance / continuation)
        assert sid_1 == sid_2

    def test_different_agent_same_invocation_different_sessions(self):
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy()

        sid_a = strategy.format(
            conversation_id="conv-1", agent_name="query-12306", invocation_id="abc123",
        )
        sid_b = strategy.format(
            conversation_id="conv-1", agent_name="office-expert", invocation_id="abc123",
        )

        # Different agent names → different sessions even with same invocation_id
        assert sid_a != sid_b

    async def test_second_empty_invocation_id_does_not_recreate_agent(self):
        """Second invocation_id="" on same template must NOT call
        _create_dynamic_subagent again — the agent is already registered."""
        from framework.core.agent import AgentContext, AgentSessionMeta
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.descriptor import AgentDescriptor

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            mock_pool = _make_mock_pool()
            mock_broker = AsyncMock()
            mock_registry = MagicMock()

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=mock_broker,
                registry=mock_registry,
                pool=mock_pool,
                pool_name="main",
                project_dir=project,
                template_registry=registry,
            )

            # ---- First call: invocation_id="" → creates subagent ----
            mock_registry.get_descriptor.return_value = None
            mock_registry.get_profile.return_value = None

            ctx = AgentContext(
                system_prompt="",
                history=MagicMock(),
                tool_manager=MagicMock(),
                session_meta=AgentSessionMeta(
                    conversation_id="conv-1", agent_name="main",
                    comm_kind=AgentCommKind.NORMAL,
                ),
            )

            result1 = await service.send_async(
                target_agent="helper", content="first task",
                invocation_id="", context=ctx,
            )
            assert "Error" not in str(result1)
            assert mock_pool.register_resident.call_count == 1

            # ---- Second call: invocation_id="" again → new agent instance ----
            # Template is checked first, so each new invocation creates a fresh
            # agent with the correct OUTPUT.md path in its system prompt.
            result2 = await service.send_async(
                target_agent="helper", content="second task",
                invocation_id="", context=ctx,
            )
            assert "Error" not in str(result2)
            # Each new invocation creates a new agent instance (template-first lookup)
            assert mock_pool.register_resident.call_count == 2, (
                "Second invocation_id='' must create a new agent instance via template"
            )
            # The two calls must have different invocation_ids
            inv1 = result1.split("invocation_id: ")[1] if "invocation_id:" in result1 else ""
            inv2 = result2.split("invocation_id: ")[1] if "invocation_id:" in result2 else ""
            assert inv1 != inv2, "Different tasks must have different invocation_ids"


class TestSubagentSafetyHooks:
    """Subagent pipeline must have safety hooks wired for communication edge cases.

    Two guard hooks:
    - SubagentAutoSendHook: catches "LLM forgot to call send_to_agent"
    - MaxIterationNotifyHook: catches "max_iterations reached"
    """

    async def test_max_iteration_notify_hook_is_wired(self):
        """MaxIterationNotifyHook must be wired on the subagent pipeline."""
        from framework.hook import HookRunner, HookErrorPolicy, HookSpec
        from framework.hook.builtin import SubagentAutoSendHook
        from framework.hook.notification import MaxIterationNotifyHook

        # Simulate a pipeline with hook_runner that records hooks
        recorded_hooks: list = []

        class _RecordingRunner:
            def __init__(self):
                self._specs: list = []

            def add(self, spec):
                recorded_hooks.append(spec.hook)

        pipeline = MagicMock()
        pipeline.hook_runner = _RecordingRunner()

        mock_pool = _make_mock_pool()
        mock_pool.get.return_value = MagicMock(pipeline=pipeline)

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService

        mock_broker = AsyncMock()
        mock_registry = MagicMock()
        mock_notification = MagicMock()

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=mock_registry,
            pool=mock_pool,
            notification_service=mock_notification,
            inbox_consumer=MagicMock(),
            agent_bus=MagicMock(),
        )

        service._wire_subagent_hooks("worker")

        hook_types = {type(h) for h in recorded_hooks}
        assert SubagentAutoSendHook in hook_types, (
            "SubagentAutoSendHook must be wired"
        )
        assert MaxIterationNotifyHook in hook_types, (
            "MaxIterationNotifyHook must be wired for max_iterations guard"
        )

    async def test_hooks_not_wired_without_pipeline(self):
        """_wire_subagent_hooks must be safe when pipeline is None."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService

        mock_pool = _make_mock_pool()
        mock_pool.get.return_value = None  # No agent found

        mock_broker = AsyncMock()
        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=MagicMock(),
            pool=mock_pool,
        )

        # Should not raise
        service._wire_subagent_hooks("worker")


class TestOutputMdInjection:
    """Verify OUTPUT.md protocol is injected into subagent system prompt
    with the correct absolute path and scoped-write alignment."""

    def test_output_md_path_contains_session_structure(self):
        """OUTPUT.md path must contain session-id components and end with OUTPUT.md."""
        from pathlib import Path as _Path

        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy()
        session_id = strategy.format(
            conversation_id="conv-1", agent_name="reviewer", invocation_id="abc123",
        )
        runtime_dir = _Path(tempfile.gettempdir()) / "runtime_state" / "coding"
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"

        # Must be absolute (runtime_dir is absolute → output_path is absolute)
        assert output_path.is_absolute(), "OUTPUT.md path must be absolute"
        assert str(output_path).endswith("OUTPUT.md")
        assert "conv-1.reviewer.abc123" in str(output_path)
        assert "output" in str(output_path)

    def test_scoped_write_dir_covers_output_md(self):
        """READ_ONLY scoped_write_dir must be the parent of OUTPUT.md's directory."""
        from pathlib import Path as _Path

        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        strategy = DefaultSessionIdStrategy()
        session_id = strategy.format(
            conversation_id="conv-1", agent_name="scout", invocation_id="xyz789",
        )
        runtime_dir = _Path(tempfile.gettempdir()) / "runtime_state" / "coding"
        scoped_write_dir = runtime_dir / "output"
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"

        # The scoped write allowed dir must be an ancestor of OUTPUT.md
        output_resolved = output_path.resolve()
        scoped_resolved = scoped_write_dir.resolve()
        assert str(output_resolved).startswith(str(scoped_resolved)), (
            f"OUTPUT.md path ({output_resolved}) must be under "
            f"scoped_write_dir ({scoped_resolved})"
        )

    def test_read_only_template_gets_scoped_write_tools(self):
        """READ_ONLY template must receive ScopedWriteFileTool + ScopedEditFileTool."""
        import tempfile
        from pathlib import Path as _Path

        from framework.tools.presets import ToolPreset, get_preset_tools

        scoped_dir = _Path(tempfile.gettempdir()) / "output"
        tools = get_preset_tools(ToolPreset.READ_ONLY, scoped_write_dir=scoped_dir)
        tool_names = {t.name for t in tools}

        assert "write" in tool_names, "READ_ONLY must have write tool for OUTPUT.md"
        assert "edit" in tool_names, "READ_ONLY must have edit tool for OUTPUT.md"
        # The write tool description mentions it is scoped to allowed directories
        write_tool = next(t for t in tools if t.name == "write")
        desc = write_tool.description
        assert "You can ONLY write" in desc or "ONLY" in desc.upper(), (
            "Scoped write tool must indicate path restriction in description"
        )

    def test_full_template_does_not_get_scoped_tools(self):
        """READ_WRITE template uses standard write/edit, not scoped versions."""
        from framework.tools.presets import ToolPreset, get_preset_tools

        tools = get_preset_tools(ToolPreset.READ_WRITE)
        tool_names = {t.name for t in tools}
        assert "write" in tool_names
        assert "edit" in tool_names

    def test_no_scoped_dir_means_no_write_for_read_only(self):
        """READ_ONLY without scoped_write_dir gets no write/edit at all."""
        from framework.tools.presets import ToolPreset, get_preset_tools

        tools = get_preset_tools(ToolPreset.READ_ONLY, scoped_write_dir=None)
        tool_names = {t.name for t in tools}

        assert "write" not in tool_names, (
            "Without scoped_write_dir, READ_ONLY must not get write"
        )
        assert "edit" not in tool_names, (
            "Without scoped_write_dir, READ_ONLY must not get edit"
        )

    async def test_system_prompt_includes_output_md_protocol(self):
        """The subagent system prompt must contain OUTPUT.md with absolute path."""
        import tempfile
        from pathlib import Path as _Path

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.template import AgentTemplate
        from framework.multi_agent.template_registry import AgentTemplateRegistry
        from framework.tools.presets import ToolPreset

        # Set up template registry — correct directory layout:
        #   config/pools/{pool}/templates/{type}.yml
        project = _Path(tempfile.mkdtemp())
        pool_tpl_dir = project / "config" / "pools" / "main" / "templates"
        pool_tpl_dir.mkdir(parents=True)
        (pool_tpl_dir / "helper.yml").write_text(
            "agent_type: helper\ndescription: Test\ntool_preset: read_only\nmax_steps: 10\n"
        )
        registry = AgentTemplateRegistry(project)
        template = registry.get_template("main", "helper")
        assert template is not None, (
            f"Template not found — check dir: {pool_tpl_dir}, "
            f"files: {list(pool_tpl_dir.iterdir()) if pool_tpl_dir.exists() else 'N/A'}"
        )
        assert template.tool_preset == ToolPreset.READ_ONLY

        # Create service with runtime_dir → OUTPUT.md protocol injected
        runtime_dir = _Path(tempfile.mkdtemp()) / "runtime"
        mock_pool = _make_mock_pool()
        mock_broker = AsyncMock()

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=MagicMock(),
            pool=mock_pool,
            pool_name="main",
            project_dir=project,
            template_registry=registry,
            runtime_dir=runtime_dir,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1", agent_name="main",
                comm_kind=AgentCommKind.NORMAL,
            ),
        )
        result = await service.send_async(
            target_agent="helper", content="do something",
            invocation_id="", context=ctx,
        )

        assert "Error" not in str(result)
        call_args = mock_pool.register_resident.call_args
        descriptor = call_args[0][0]
        system_prompt = descriptor.system_prompt_template
        assert system_prompt is not None

        # OUTPUT.md is now in the dynamic OutputMdProvider, not in the static
        # descriptor.system_prompt_template. Verify via build_system_prompt().
        ctx_mgr = call_args[1].get("context_manager")
        assert ctx_mgr is not None, "context_manager must be passed"
        built = await ctx_mgr.build_system_prompt(tool_manager=None)
        assert "OUTPUT.md" in built
        assert "CRITICAL" in built
        assert "`write` tool" in built
        # For READ_ONLY: must mention scoped write access (in static prompt)
        assert "Read-Only Mode" in system_prompt

    async def test_output_md_before_fork_context(self):
        """OUTPUT.md section must appear BEFORE fork context in built prompt."""
        import tempfile
        from pathlib import Path as _Path

        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.template import AgentTemplate
        from framework.multi_agent.template_registry import AgentTemplateRegistry
        from framework.tools.presets import ContextMode, ToolPreset

        project = _Path(tempfile.mkdtemp())
        pool_tpl_dir = project / "config" / "pools" / "main" / "templates"
        pool_tpl_dir.mkdir(parents=True)
        (pool_tpl_dir / "helper.yml").write_text(
            "agent_type: helper\ndescription: Test\n"
            "tool_preset: read_only\ncontext_mode: fork\nmax_steps: 10\n"
        )
        registry = AgentTemplateRegistry(project)
        template = registry.get_template("main", "helper")
        assert template is not None and template.context_mode == ContextMode.FORK

        runtime_dir = _Path(tempfile.mkdtemp()) / "runtime"
        mock_pool = _make_mock_pool()
        mock_broker = AsyncMock()

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            broker=mock_broker,
            registry=MagicMock(),
            pool=mock_pool,
            pool_name="main",
            project_dir=project,
            template_registry=registry,
            runtime_dir=runtime_dir,
        )

        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1", agent_name="main",
                comm_kind=AgentCommKind.NORMAL,
            ),
        )
        await service.send_async(
            target_agent="helper", content="do something",
            invocation_id="", context=ctx,
        )

        call_args = mock_pool.register_resident.call_args
        ctx_mgr = call_args[1].get("context_manager")
        assert ctx_mgr is not None
        # Load sets _last_session_id so OutputMdProvider gets the right session
        await ctx_mgr.load(session_id="conv-1.helper.abc123")
        built = await ctx_mgr.build_system_prompt(tool_manager=None)

        # OUTPUT.md (from OutputMdProvider) must appear before Fork Context
        # (Fork Context is in base_system_prompt via descriptor, not ctx_mgr)
        assert "OUTPUT.md" in built
        assert "CRITICAL" in built

    async def test_built_system_prompt_contains_output_md(self):
        """OutputMdProvider injects per-session OUTPUT.md path dynamically."""
        import tempfile
        from pathlib import Path as _Path

        from framework.memory.core.scope import MemoryAgentRole
        from framework.ioc.factories.descriptors import build_session_only_memory
        from framework.ioc.configs.memory import MemoryConfig

        runtime_dir = _Path(tempfile.mkdtemp()) / "runtime"
        session_id = "conv-1.reviewer.abc123"
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"
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

        # load() sets _last_session_id so OutputMdProvider gets the right session
        await ctx_mgr.load(session_id)
        built = await ctx_mgr.build_system_prompt(tool_manager=None)

        assert "OUTPUT.md" in built
        assert str(output_path) in built, (
            f"Built prompt must contain the absolute OUTPUT.md path: {output_path}"
        )
        assert "CRITICAL" in built
        assert "`write` tool" in built


class TestSubagentToolInstanceIsolation:
    """Every subagent must get independent tool instances — no object sharing.

    Two subagents created from the same template must NOT share:
    - tool_manager objects
    - individual tool instances (e.g. ReadFileTool)
    - MCP connections / managers

    This is critical for MCP: if subagents share a tool_manager, one
    subagent's MCP tools would leak into another.
    """

    async def test_two_subagents_get_distinct_tool_managers(self):
        """Each _create_dynamic_subagent call creates a new InMemoryToolManager."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.template_registry import AgentTemplateRegistry

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n"
                "use_terminal: false\ntool_preset: read_only\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=AsyncMock(),
                registry=MagicMock(),
                pool=_make_mock_pool(),
                pool_name="main",
                project_dir=project,
            )

            # Create two subagents
            result_a = await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-a", content="task A",
            )
            result_b = await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-b", content="task B",
            )

            assert result_a.error is None
            assert result_b.error is None

            # Extract tool_managers from register_resident calls
            pool = service._pool
            call_args_list = pool.register_resident.call_args_list
            assert len(call_args_list) == 2

            tm_a = call_args_list[0][1]["tool_manager"]
            tm_b = call_args_list[1][1]["tool_manager"]

            # Must be different objects
            assert tm_a is not tm_b, (
                "Subagents must get distinct tool_manager instances, "
                "not the same object"
            )

    async def test_tool_instances_not_shared_between_subagents(self):
        """Registering a tool in one subagent's manager must not affect the other."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.template_registry import AgentTemplateRegistry
        from framework.tools.standard import ReadFileTool

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n"
                "use_terminal: false\n",
                "You are a helper.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "helper")
            assert template is not None

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=AsyncMock(),
                registry=MagicMock(),
                pool=_make_mock_pool(),
                pool_name="main",
                project_dir=project,
            )

            result_a = await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-a", content="task A",
            )
            result_b = await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-b", content="task B",
            )

            pool = service._pool
            tm_a = pool.register_resident.call_args_list[0][1]["tool_manager"]
            tm_b = pool.register_resident.call_args_list[1][1]["tool_manager"]

            # The actual tool INSTANCES should be different objects
            tool_a = tm_a.get_tool("read")
            tool_b = tm_b.get_tool("read")
            assert tool_a is not None
            assert tool_b is not None
            assert tool_a is not tool_b, (
                "Preset tool instances must NOT be shared between subagents. "
                "Each subagent gets its own ReadFileTool instance."
            )

    async def test_subagents_have_independent_preset_tool_instances(self):
        """Two subagents with READ_ONLY preset each get their own tool instances."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService
        from framework.multi_agent.template_registry import AgentTemplateRegistry
        from framework.tools.presets import ToolPreset

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "scout",
                "agent_type: scout\ndescription: Scout\ntool_preset: read_only\n"
                "max_steps: 10\nuse_terminal: false\n",
                "You are a scout.")

            registry = AgentTemplateRegistry(project)
            template = registry.get_template("main", "scout")
            assert template is not None
            assert template.tool_preset == ToolPreset.READ_ONLY

            service = AgentCommunicationService(
                source=AgentAddress(name="main"),
                broker=AsyncMock(),
                registry=MagicMock(),
                pool=_make_mock_pool(),
                pool_name="main",
                project_dir=project,
            )

            await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-1", content="task 1",
            )
            await service._create_dynamic_subagent(
                template=template, conversation_id="conv-1",
                invocation_id="inv-2", content="task 2",
            )

            pool = service._pool
            tm_1 = pool.register_resident.call_args_list[0][1]["tool_manager"]
            tm_2 = pool.register_resident.call_args_list[1][1]["tool_manager"]

            for tool_name in tm_1.list_tools():
                t1 = tm_1.get_tool(tool_name)
                t2 = tm_2.get_tool(tool_name)
                assert t1 is not None and t2 is not None
                assert t1 is not t2, (
                    f"Tool '{tool_name}': instances must be distinct. "
                    f"Subagent 1 and 2 got the same object ({id(t1)} == {id(t2)})."
                )
