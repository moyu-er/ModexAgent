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

            mock_pool = AsyncMock()
            mock_pool.register_resident = AsyncMock()
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

            mock_pool = AsyncMock()
            mock_pool.register_resident = AsyncMock()
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

            mock_pool = AsyncMock()
            mock_pool.register_resident = AsyncMock()
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

            mock_pool = AsyncMock()
            mock_pool.register_resident = AsyncMock()
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
            assert "write_file" in tool_names
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
