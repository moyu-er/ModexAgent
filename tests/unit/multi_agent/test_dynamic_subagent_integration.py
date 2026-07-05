"""Integration test: template → registry → system prompt resolution → XML messages."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.message_xml import build_agent_message, build_agent_result
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry


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
            "agent_name: helper\ndescription: Test helper\nmax_steps: 15\n",
            "You are a helpful assistant."
        )

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 1

        t = templates[0]
        assert t.agent_name == "helper"
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
        _write_files(project, "main", "a", "agent_name: a\ndescription: ''\n", "A")
        _write_files(project, "main", "b", "agent_name: b\ndescription: ''\n", "B")

        registry = AgentTemplateRegistry(project)
        templates = registry.list_templates("main")
        assert len(templates) == 2
        types = {t.agent_name for t in templates}
        assert types == {"a", "b"}


def test_template_memory_is_baked_not_from_yaml():
    """A template carrying a ``memory:`` block is REJECTED — subagent memory is
    baked (sub-minimal, immutable, spec §9). The factory's default is the sole
    source of truth; a stale/hand-edited rich block can never override it."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        yml = """\
agent_name: heavy
description: Heavy task agent
max_steps: 50
memory:
  short_term: {max_context_tokens: 50000}
"""
        _write_files(project, "main", "heavy", yml, "Heavy agent.")
        # A memory block is no longer an accepted key → load fails loud (logged)
        # and the template is not registered.
        registry = AgentTemplateRegistry(project)
        assert registry.get_template("main", "heavy") is None


def test_template_memory_baked_from_factory_default():
    """A template WITHOUT a memory block gets the factory's baked preset,
    identity-equal (the loader stores it directly, never re-validates)."""
    from modex_agent.ioc.configs.memory import MemoryConfig

    baked = MemoryConfig()
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        yml = "agent_name: light\ndescription: light\n"
        _write_files(project, "main", "light", yml, "Light agent.")
        registry = AgentTemplateRegistry(project, default_subagent_memory=baked)
        t = registry.get_template("main", "light")
        assert t is not None
        assert t.memory is baked


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
        from modex_agent.multi_agent.communication import AgentCommunicationService
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.core.agent import AgentContext

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
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
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
        from modex_agent.multi_agent.communication import AgentCommunicationService
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.core.agent import AgentContext

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
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
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
        from modex_agent.multi_agent.tools import _NORMAL_PARAMS

        desc = _NORMAL_PARAMS["properties"]["invocation_id"]["description"].lower()
        assert "normal" not in desc
        assert "subagent" not in desc

    def test_tool_description_no_kind_mention(self):
        from modex_agent.multi_agent.tools import CommunicationTargetStore, SendToAgentTool

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
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.multi_agent.communication import AgentCommunicationService
        from modex_agent.multi_agent.address import AgentAddress

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
            session=SessionInfo.from_str("conv-1.helper"),
            comm_kind=AgentCommKind.SUBAGENT,
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
        from modex_agent.core.agent import AgentContext
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.communication import AgentCommunicationService
        from modex_agent.multi_agent.descriptor import AgentDescriptor

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
            session=SessionInfo.from_str("conv-1.main"),
            comm_kind=AgentCommKind.NORMAL,
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

    Session isolation is driven by invocation_id via SessionIdFactory.
    Same external_id = same session (memory inheritance).
    Different external_id = different session (fresh context).
    """

    def test_same_agent_different_invocation_produces_different_sessions(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_a = factory.create(
            agent_name="query-12306", external_id="abc123",
        )
        sid_b = factory.create(
            agent_name="query-12306", external_id="def456",
        )

        # Different invocation_ids must produce different session IDs
        assert str(sid_a) != str(sid_b)
        assert sid_a.agent_name == "query-12306"
        assert sid_b.agent_name == "query-12306"

    def test_same_invocation_produces_same_session(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_1 = factory.create(
            agent_name="query-12306", external_id="abc123",
        )
        sid_2 = factory.create(
            agent_name="query-12306", external_id="abc123",
        )

        # Same external_id → same snowflake → same session (memory inheritance)
        assert str(sid_1) == str(sid_2)

    def test_different_agent_same_invocation_different_sessions(self):
        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()

        sid_a = factory.create(
            agent_name="query-12306", external_id="abc123",
        )
        sid_b = factory.create(
            agent_name="office-expert", external_id="abc123",
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

    # ADR-0015 D3: test_max_iteration_notify_hook_is_wired deleted.
    # ADR-0015 D3: test_hooks_not_wired_without_pipeline deleted.


class TestOutputMdInjection:
    """Verify OUTPUT.md protocol is injected into subagent system prompt
    with the correct absolute path and scoped-write alignment."""

    def test_output_md_path_contains_session_structure(self):
        """OUTPUT.md path must contain session-id components and end with OUTPUT.md."""
        from pathlib import Path as _Path

        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()
        session = factory.create(
            agent_name="reviewer", external_id="abc123",
        )
        session_id = session.session_id
        runtime_dir = _Path(tempfile.gettempdir()) / "runtime_state" / "coding"
        output_path = runtime_dir / "output" / session_id / "OUTPUT.md"

        # Must be absolute (runtime_dir is absolute → output_path is absolute)
        assert output_path.is_absolute(), "OUTPUT.md path must be absolute"
        assert str(output_path).endswith("OUTPUT.md")
        assert ".reviewer" in str(output_path)
        assert "output" in str(output_path)

    def test_scoped_write_dir_covers_output_md(self):
        """READ_ONLY scoped_write_dir must be the parent of OUTPUT.md's directory."""
        from pathlib import Path as _Path

        from modex_agent.core.session_id import SessionIdFactory

        factory = SessionIdFactory()
        session = factory.create(
            agent_name="scout", external_id="xyz789",
        )
        session_id = session.session_id
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

        from modex_agent.tools.presets import ToolPreset, get_preset_tools

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
        from modex_agent.tools.presets import ToolPreset, get_preset_tools

        tools = get_preset_tools(ToolPreset.READ_WRITE)
        tool_names = {t.name for t in tools}
        assert "write" in tool_names
        assert "edit" in tool_names

    def test_no_scoped_dir_means_no_write_for_read_only(self):
        """READ_ONLY without scoped_write_dir gets no write/edit at all."""
        from modex_agent.tools.presets import ToolPreset, get_preset_tools

        tools = get_preset_tools(ToolPreset.READ_ONLY, scoped_write_dir=None)
        tool_names = {t.name for t in tools}

        assert "write" not in tool_names, (
            "Without scoped_write_dir, READ_ONLY must not get write"
        )
        assert "edit" not in tool_names, (
            "Without scoped_write_dir, READ_ONLY must not get edit"
        )

    # ADR-0015 D3: test_system_prompt_includes_output_md_protocol deleted —
    # prompt assembly moved into AgentTemplate.materialize. OUTPUT.md injection
    # is covered by test_built_system_prompt_contains_output_md below.
    # ADR-0015 D3: test_output_md_before_fork_context deleted — same reason;
    # OUTPUT.md-before-fork ordering is asserted by the OutputMdProvider
    # ordering exercised in test_built_system_prompt_contains_output_md.

    async def test_built_system_prompt_contains_output_md(self):
        """OutputMdProvider injects per-session OUTPUT.md path dynamically."""
        import tempfile
        from pathlib import Path as _Path

        from modex_agent.core.scope import MemoryAgentRole
        from modex_agent.ioc.factories.descriptors import build_session_only_memory
        from modex_agent.ioc.configs.memory import MemoryConfig

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
    """ADR-0015 D3: _create_dynamic_subagent folded into AgentTemplate.materialize;
    the tool_manager is now built fresh inside each materialize() call, so
    distinct-by-construction holds. The three original tests are deleted."""

    # ADR-0015 D3: test_two_subagents_get_distinct_tool_managers deleted.
    # ADR-0015 D3: test_tool_instances_not_shared_between_subagents deleted.
    # ADR-0015 D3: test_subagents_have_independent_preset_tool_instances deleted.

