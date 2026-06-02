"""Integration test: template → registry → system prompt resolution → XML messages."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


class TestListTargetsShowsTemplatesWithNoRegisteredAgents:
    """Bug: list_communication_targets returns early when no registered targets,
    preventing template discovery from being shown.

    When only the main agent is registered and templates exist, the tool must
    still show template entries (not return "No other agents are currently available").
    """

    async def test_templates_shown_when_no_registered_targets(self):
        from framework.multi_agent.address import AgentAddress

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "office-expert",
                "agent_type: office-expert\ndescription: Office tasks\n",
                "Office expert.")
            _write_files(project, "main", "query-12306",
                "agent_type: query-12306\ndescription: Train tickets\n",
                "Train ticket agent.")

            registry = AgentTemplateRegistry(project)
            from framework.multi_agent.registry import AgentProfile

            mock_reg = MagicMock()
            mock_reg.list_profiles.return_value = [
                AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
            ]

            from framework.multi_agent.tools import ListCommunicationTargetsTool
            tool = ListCommunicationTargetsTool(
                self_address=AgentAddress(name="main"),
                registry=mock_reg,
                template_registry=registry,
                pool_name="main",
            )

            result = await tool.execute()

            # Must NOT return the early-exit message
            assert "No other agents" not in result
            assert "office-expert" in result
            assert "query-12306" in result


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
        from framework.multi_agent.tools import _INVOCATION_ID_PARAM

        desc = _INVOCATION_ID_PARAM["description"].lower()
        assert "normal" not in desc
        assert "subagent" not in desc

    def test_tool_description_no_kind_mention(self):
        from framework.multi_agent.tools import SendToAgentTool

        service = MagicMock()
        service.build_targets_description.return_value = "Targets available."
        tool = SendToAgentTool(
            source=MagicMock(),
            broker=MagicMock(),
            registry=MagicMock(),
            agent_bus=MagicMock(),
            service=service,
        )

        desc = tool.description.lower()
        assert "normal" not in desc
        assert "subagent" not in desc


class TestListTargetsHidesKindFromInvocationGuidance:
    """list_communication_targets should not give kind-specific invocation_id rules."""

    async def test_no_kind_specific_invocation_rules(self):
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.tools import ListCommunicationTargetsTool
        from framework.multi_agent.registry import AgentProfile

        mock_reg = MagicMock()
        mock_reg.list_profiles.return_value = [
            AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
            AgentProfile(name="other", comm_kind=AgentCommKind.NORMAL),
        ]

        tool = ListCommunicationTargetsTool(
            self_address=AgentAddress(name="main"),
            registry=mock_reg,
        )

        result = await tool.execute()

        # Should not say "MUST be null" (kind-specific rule)
        assert "MUST be null" not in result


class TestSubagentIdentityResolution:
    """Bug: communication tools hardcode source="main", causing subagents to
    use wrong identity. When a subagent calls list_communication_targets, it
    must filter out SUBAGENT targets (only see NORMAL). When it sends, envelope
    source must be the subagent name, not "main".
    """

    async def test_subagent_list_targets_filters_subagents(self):
        """Subagent calling list_communication_targets should NOT see other SUBAGENT targets."""
        from framework.core.agent import AgentContext, current_agent_context, AgentSessionMeta
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.tools import ListCommunicationTargetsTool
        from framework.multi_agent.registry import AgentProfile

        mock_reg = MagicMock()
        mock_reg.list_profiles.return_value = [
            AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
            AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT),
            AgentProfile(name="query-12306", comm_kind=AgentCommKind.SUBAGENT),
        ]

        tool = ListCommunicationTargetsTool(
            self_address=AgentAddress(name="main"),
            registry=mock_reg,
        )

        # Simulate subagent context — the tool must detect it's a subagent
        ctx = AgentContext(
            system_prompt="",
            history=MagicMock(),
            tool_manager=MagicMock(),
            session_meta=AgentSessionMeta(
                conversation_id="conv-1",
                agent_name="office-expert",
                comm_kind=AgentCommKind.SUBAGENT,
            ),
        )
        token = current_agent_context.set(ctx)
        try:
            result = await tool.execute()

            # Subagent should only see NORMAL targets
            assert "main" in result
            assert "query-12306" not in result  # other subagent, filtered
        finally:
            current_agent_context.reset(token)

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
        from framework.core.context import InMemoryContextManager
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

            main_ctx = InMemoryContextManager(base_system_prompt="Main system prompt")

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
            assert passed_ctx is not main_ctx, (
                "Must be a fresh instance, not pool's default (main's) context manager"
            )

    async def test_subagent_gets_dedicated_tool_manager(self):
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.communication import AgentCommunicationService

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "helper",
                "agent_type: helper\ndescription: Test\nmax_steps: 10\n"
                "standard_tools: true\nuse_terminal: false\n",
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
            assert "send_to_agent" in tool_names
            assert "list_communication_targets" in tool_names
            assert "read_file" in tool_names
            assert "write" in tool_names
            assert "mcp_playwright_browser_navigate" not in tool_names, (
                "Subagent must not inherit main's MCP tools"
            )

    async def test_subagent_list_targets_excludes_templates(self):
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.registry import AgentProfile

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _write_files(project, "main", "office-expert",
                "agent_type: office-expert\ndescription: Office tasks\n",
                "Office expert.")
            _write_files(project, "main", "query-12306",
                "agent_type: query-12306\ndescription: Train tickets\n",
                "Train tickets.")

            registry = AgentTemplateRegistry(project)

            mock_reg = MagicMock()
            mock_reg.list_profiles.return_value = [
                AgentProfile(name="query-12306", comm_kind=AgentCommKind.SUBAGENT),
                AgentProfile(name="main", comm_kind=AgentCommKind.NORMAL),
                AgentProfile(name="office-expert", comm_kind=AgentCommKind.SUBAGENT),
            ]

            from framework.multi_agent.tools import ListCommunicationTargetsTool
            tool = ListCommunicationTargetsTool(
                self_address=AgentAddress(name="query-12306"),
                registry=mock_reg,
                template_registry=registry,
                pool_name="main",
            )

            result = await tool.execute()

            assert "main" in result
            assert "office-expert" not in result, (
                "Subagent must not see unrelated templates"
            )
            assert "[template]" not in result, (
                "Subagent must not see template creation section"
            )


class TestSubagentMemoryCorrectness:
    """Dynamic subagent must get a real MemorySystemContextManager with
    session-scoped memory (no knowledge layer), not bare InMemoryContextManager.

    Verifies the subagent's context_manager is a MemorySystemContextManager
    wrapping a MemorySystem with session+archive layers (no knowledge).
    """

    async def test_subagent_gets_memory_system_context_manager(self):
        """Subagent must use MemorySystemContextManager, not InMemoryContextManager."""
        from framework.memory.system import MemorySystemContextManager
        from framework.core.context import InMemoryContextManager
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
            assert not isinstance(passed_ctx, InMemoryContextManager), (
                "Must not use bare InMemoryContextManager (no memory persistence)"
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
        assert sid_a == "conv-1:query-12306:abc123"
        assert sid_b == "conv-1:query-12306:def456"

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

            # ---- Second call: invocation_id="" again → agent already registered ----
            # Simulate: helper is now registered in registry
            mock_registry.get_descriptor.return_value = AgentDescriptor(
                address=AgentAddress(name="helper"),
                comm_kind=AgentCommKind.SUBAGENT,
            )

            result2 = await service.send_async(
                target_agent="helper", content="second task",
                invocation_id="", context=ctx,
            )
            assert "Error" not in str(result2)
            # Must NOT call register_resident again
            assert mock_pool.register_resident.call_count == 1, (
                "Second invocation_id='' must not re-create already-registered agent"
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
