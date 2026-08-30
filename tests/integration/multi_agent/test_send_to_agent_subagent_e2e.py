"""End-to-end: send_to_agent → Drainer-spawner materialize → subagent real turn (mock LLM) → hook-owned OUTPUT_<n>.md.

Critical regression guard for the subagent deliverable contract (T1-T8 refactor).
Walks the full multi-agent chain with only the LLM swapped for a scripted mock:

    main agent
      └─ AgentCommunicationService.send_async (pure router, ADR-0015 D3)
           └─ bus.send → Drainer-spawner lazy-materializes on first drain
                ├─ AgentTemplate.materialize reads agents/helper.md as subagent system prompt
                ├─ DefaultAgentFactory builds real ReActAgent + AgentPipeline
                │     (factory wrap attaches workspace_manager + pool_name)
                └─ broker / bus delivers task_request
      └─ Drainer → pipeline.process_message → real react turn
           └─ mock LLM returns a final text reply (the deliverable)

The subagent's final reply text IS the deliverable. SubagentAutoSendHook fires on
FINALLY_GRAPH, captures the reply, writes it to ``output/<session_id>/OUTPUT_<n>.md``
(numbered via max+1 scan), and sends a truncated (≤300 chars) notification to the
parent's inbox via the bus. The notification carries the output path via ResultMeta.

Assertion 1 locks the subagent isolation guard: if removed, the subagent adopts the
main agent's context_manager (``MAIN PROMPT``), so its system prompt becomes the
main's and the deliverable contract breaks.

Assertion 2 verifies OutputMdProvider is no longer injected (deprecated in T5) —
the subagent system prompt must NOT contain "OUTPUT.md".

Assertion 3 verifies the hook wrote OUTPUT_<n>.md with the subagent's reply text.

Assertion 4 verifies the parent inbox received the notification with status, output
path, and the deliverable text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.types import LLMResponse
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTarget

pytestmark = pytest.mark.integration


def _tgt(name: str, kind: AgentCommKind) -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=kind)


# ---------------------------------------------------------------------------
# Fake workspace (mirrors bot layer's Workspace.pool_data shape)
# ---------------------------------------------------------------------------


class _FakePoolData:
    """Stands in for bot.service.workspace.PoolData.

    ``context_manager`` is deliberately the MAIN agent's (a sentinel prompt) so
    the test can detect whether the subagent pipeline wrongly adopted it.
    ``turn_store`` is a real store rooted at the workspace
    runtime dir — the subagent shares it (pool-level) so its AgentRuntime is
    constructed and FINALLY_GRAPH hooks fire.
    """

    def __init__(self, runtime_dir: Path, memory_dir: Path, main_ctx_mgr: Any) -> None:
        from modex_agent.agents.react.state import ReActRuntimeStateCodec
        from modex_agent.runtime.codec import RuntimeStateCodecRegistry
        from modex_agent.runtime.enums import AgentKind
        from modex_agent.runtime.store import (
            JsonFileTurnStateStore,
        )

        self.runtime_dir = runtime_dir
        self.memory_dir = memory_dir
        self.pruned_manager = None
        self.trace_store = None  # subagent e2e test: pipeline reads but doesn't trace
        self.context_manager = main_ctx_mgr
        codec = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        self.turn_store = JsonFileTurnStateStore(runtime_dir / "turns", codec)


class _FakeWorkspace:
    def __init__(
        self, pool_data: dict[str, _FakePoolData], workspace_root: Path | None = None
    ) -> None:
        self.pool_data = pool_data
        # Satisfies WorkspaceResources.workspace_root — process_locked binds it
        # per turn. Defaults to cwd when the test doesn't care about it.
        self.workspace_root = workspace_root if workspace_root is not None else Path.cwd()


class _FakeWorkspaceManager:
    """The subagent inherits workspace and pool from the calling agent's runtime
    context — both resolve_workspace and pool_name come from the main agent's
    pipeline configuration, wired by the bot layer's DefaultAgentFactory wrap.
    """

    def __init__(self, ws: _FakeWorkspace) -> None:
        self._ws = ws

    def resolve_workspace(self) -> _FakeWorkspace:
        return self._ws


# ---------------------------------------------------------------------------
# Scripted LLM provider — returns final reply text (the deliverable)
# ---------------------------------------------------------------------------


class _ScriptedProvider(CallbackStreamProvider):
    """Mock LLM that behaves like a well-behaved subagent.

    On the single call it returns a final text reply that IS the deliverable.
    The hook (SubagentAutoSendHook) captures this reply and writes
    ``OUTPUT_<n>.md`` — the subagent does not write any file itself.
    Every call records the system prompt it saw so the test can assert which
    agent's prompt was actually used.
    """

    DELIVERABLE_TEXT: str = "Here is my deliverable: the task is complete."

    def __init__(self) -> None:
        self.call_count = 0
        self.seen_system_prompts: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        sys_text = str(sys_msg.get("content", "")) if sys_msg else ""
        self.seen_system_prompts.append(sys_text)

        return LLMResponse(content=self.DELIVERABLE_TEXT, finish_reason="stop")

    async def chat_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return await self.chat(messages, **kwargs)

    def get_default_model(self) -> str:
        return "mock-model"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_to_agent_runs_subagent_with_own_prompt_and_writes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # template.materialize unconditionally calls resolve_modexctl_bin_dir() for
    # NativeEnvInjectionHook wiring. Provide a dummy binary so materialize
    # doesn't fail in environments without modexctl installed.
    fake_bin_dir = tmp_path / "fake_bin"
    fake_bin_dir.mkdir()
    (fake_bin_dir / "modexctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(fake_bin_dir))

    # --- project skeleton: agents/<type>.md + declaration-compiled template ---
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    (project / "agents" / "helper.md").write_text(
        "You are `helper`, a test subagent. Complete the task and provide "
        "your result in your final reply.\n",
        encoding="utf-8",
    )
    # Ticket 11: the declaration is the single template source — the template
    # is compiled from a two-agent scope declaration and seeded into the
    # registry exactly as the declaration road does.
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.scope.compiler import compile_scope
    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
    from modex_agent.workspace.context import WorkspaceContext
    from modex_agent.workspace.paths import WorkspacePaths

    declared_helper = AgentSpec(
        name="helper", parent="main", description="Test helper", max_steps=5
    )
    declared_pool = PoolSpec(name="main", agents=[AgentSpec(name="main"), declared_helper])

    # --- component registry (the capability compile input, ticket 12) ---
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
    from modex_agent.plugins.registry import ComponentRegistry

    component_registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        component_registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(),
        ),
    )

    # The declaration compiles WITH the registry: the `subagents`
    # capability's tree predicate contributes the derived communication
    # entries + the `subagent_auto_send` roster hook for the non-root
    # helper (the retired compiler hard-coding died with ADR-0047) —
    # compiling registry-less would leave the helper hook-less and the
    # OUTPUT_<n>.md deliverable would never be written.
    compilation = compile_scope(
        ScopeSpec(kind=ScopeKind.POOL, pool=declared_pool),
        workspace_ctx=WorkspaceContext(
            target=project,
            paths=WorkspacePaths(root=project / ".modex"),
            is_home=False,
        ),
        registry=component_registry,
    )
    compiled_helper = next(
        agent for agent in compilation.agents if agent.provenance.agent == "helper"
    )
    template_registry = AgentTemplateRegistry(
        seeded={
            "main": {
                "helper": AgentTemplate(
                    spec=declared_helper,
                    toolset_profile=compiled_helper.defaults.toolset_profile,
                    compiled_spec=compiled_helper.spec,
                )
            }
        }
    )

    # --- workspace (fake) ---
    runtime_dir = tmp_path / "workspace" / "runtime_state" / "main"
    memory_dir = tmp_path / "workspace" / "memory" / "main"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    main_ctx_mgr = InMemoryContextManager(
        base_system_prompt="MAIN PROMPT — must NOT leak into subagent"
    )
    ws = _FakeWorkspace(
        {"main": _FakePoolData(runtime_dir, memory_dir, main_ctx_mgr)},
        workspace_root=tmp_path / "workspace",
    )
    workspace_manager = _FakeWorkspaceManager(ws)

    # --- broker / inbox / bus ---
    broker = InMemoryMessageBroker()
    await broker.start()
    inbox_server = InMemoryInboxServer()
    producer = InboxProducer(server=inbox_server)
    consumer = InboxConsumer(server=inbox_server)
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer)

    provider = _ScriptedProvider()

    # --- factory, wrapped to attach workspace_manager + pool_name (bot-style) ---
    from modex_agent.hook import HookRunner

    shared_hook_runner = HookRunner()
    factory = DefaultAgentFactory(
        default_llm_provider=provider,
        default_hook_runner=shared_hook_runner,
    )

    original_create = factory.create_agent

    async def _create_then_wire_workspace(*args: Any, **kwargs: Any) -> Any:
        instance = await original_create(*args, **kwargs)
        if instance.pipeline is not None:
            instance.pipeline._turn_runner._workspace_manager = workspace_manager  # type: ignore[attr-defined]
            instance.pipeline._turn_runner._pool_name = "main"  # type: ignore[attr-defined]
        return instance

    factory.create_agent = _create_then_wire_workspace  # type: ignore[method-assign]

    session_registry = InMemorySessionRegistry()
    session_factory = SessionIdFactory()
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=bus,
        inbox_consumer=consumer,
        session_factory=session_factory,
        retention=SessionRetentionPolicy(),
        session_registry=session_registry,
    )
    # Poll-driven cutover (Task 8): the InboxPoller is the sole between-turn
    # driver. The production wiring (create_pool, Task 7) builds + attaches +
    # starts one per pool; this test builds its pool directly, so it must mirror
    # that wiring. The old signal-callback path (pool._on_inbox_signal) is now
    # a no-op stub.
    from modex_agent.multi_agent.inbox_poller import InboxPoller

    poller = InboxPoller(pool, interval=0.05)
    pool.attach_poller(poller)
    pool.start_poller()

    # --- materialize deps + template registry (mirrors bot pool_builder) ---
    # ADR-0015 D5: subagent construction moved into AgentTemplate.materialize,
    # driven by a bundled AgentMaterializeDeps + scope path. The
    # service is now a pure router; send_async enqueues and the Drainer-spawner
    # materializes on first drain.
    from modex_agent.multi_agent.context_fork import ContextForkBuilder
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.workspace.scope_path import ScopePath

    scope_path = ScopePath(workspace_root=tmp_path / "workspace", pool_name="main")
    context_fork_builder = ContextForkBuilder()

    # SessionTreeManager with InMemory stores — real tree for the
    # SubagentAutoSendHook deliver path (todo 20 fix: tree= is mandatory
    # on AgentMaterializeDeps since todo 16).
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
    from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
    from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore

    tree_manager = SessionTreeManager(
        tree_store=InMemorySessionTreeStore(),
        node_store=InMemoryTreeNodeStore(),
        track_store=InMemoryMessageTrackStore(),
        bus=bus,
        poller=poller,
        pool_name="main",
        workspace_root=str(tmp_path / "workspace"),
        session_registry=session_registry,
    )
    consumer.set_on_consumed(tree_manager.on_consumed)
    poller.attach_tree_manager(tree_manager)

    # The pool assembly context the roster hook factories derive from
    # (SubagentAutoSendHook's declared parent + runtime_dir, native_env's
    # env spec) plus the pool's aggregated `subagents` capability supply —
    # the same faces create_pool threads onto AgentMaterializeDeps in
    # production. pool_data is the fake snapshot (runtime_dir-backed), so
    # the auto-send hook writes OUTPUT_<n>.md under the workspace's
    # runtime_state/main/output — not the CWD fallback.
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
    from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsCapability

    pool_assembly_ctx = PoolAssemblyContext(
        pool_name="main",
        pool_spec=declared_pool,
        project_dir=project,
        data_dir=tmp_path / "workspace",
        broker=broker,
        inbox_server=inbox_server,
        agent_bus=bus,
        output_adapter=None,
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=TurnSessionRegistry(),
        pool_data=ws.pool_data["main"],
    )
    subagents_supply = SubagentsCapability().supply(
        PoolSupplyView(
            pool_name="main",
            entries=(PoolSupplyAgentEntry(agent_name="helper", config={}),),
            root_agent_name="main",
            pool=pool,
            session_tree=tree_manager,
            project_dir=project,
        )
    )
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=session_factory,
        broker=broker,
        tree=tree_manager,
        # Must mirror production wiring (bot resources.py) — TurnRunner reads safety.turn.
        safety=RuntimeSafetyPolicy(),
        llm_provider=provider,
        project_dir=project,
        agent_bus=bus,
        context_fork_builder=context_fork_builder,
        scope_path=scope_path,
        workspace_manager=workspace_manager,
        component_registry=component_registry,
        pool_assembly_ctx=pool_assembly_ctx,
        capability_supply={"subagents": subagents_supply},
    )
    pool.materialize_deps = deps
    pool.template_registry = template_registry
    pool.pool_name = "main"
    pool.context_fork_builder = context_fork_builder

    # --- communication service for the main agent (pure router) ---
    service = AgentCommunicationService(
        source=AgentAddress(name="main"),
        tree=tree_manager,
        registry=pool,
        session_factory=session_factory,
        session_registry=session_registry,
        template_registry=template_registry,
        pool=pool,
        pool_name="main",
        project_dir=project,
    )

    try:
        ctx = AgentContext(
            system_prompt="",
            history=None,  # type: ignore[arg-type]
            tool_manager=None,  # type: ignore[arg-type]
            session=session_factory.create(agent_name="main"),
            comm_kind=AgentCommKind.NORMAL,
        )

        ack = await service.send_async(
            target=_tgt("helper", AgentCommKind.SUBAGENT),
            content="Produce the deliverable.",
            invocation_id="",
            context=ctx,
        )
        assert "Error" not in ack, f"send_async failed: {ack}"

        # Wait for the subagent turn to finish. The Drainer-spawner
        # materializes the helper instance lazily on first drain, then runs the
        # turn. Poll for: (1) the single LLM call, AND (2) the hook-written
        # OUTPUT_<n>.md file appearing under runtime_dir/output — the file
        # signals FINALLY_HOOK has fired (synchronous write before the bus
        # notification).
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            output_files = (
                list((runtime_dir / "output").rglob("OUTPUT_*.md"))
                if (runtime_dir / "output").exists()
                else []
            )
            if provider.call_count >= 1 and output_files:
                break
            await asyncio.sleep(0.05)

        # --- assertion 1: subagent used its OWN system prompt (the guard) ---
        assert provider.seen_system_prompts, "subagent LLM was never invoked"
        subagent_system = provider.seen_system_prompts[0]
        assert "You are `helper`" in subagent_system, (
            "subagent system prompt is not helper.md — the pipeline likely "
            "overrode it with the main agent's context_manager"
        )
        assert "MAIN PROMPT" not in subagent_system, (
            "main agent prompt leaked into the subagent — the ctx_mgr guard "
            "in _process_message_locked is missing"
        )

        # --- assertion 2: OutputMdProvider deprecated — no OUTPUT.md in prompt ---
        assert "OUTPUT.md" not in subagent_system, (
            "OutputMdProvider is deprecated (T5) but OUTPUT.md still appears "
            "in the subagent system prompt — the provider is still registered"
        )

        # --- assertion 3: hook wrote OUTPUT_<n>.md with the deliverable text ---
        output_files = list((runtime_dir / "output").rglob("OUTPUT_*.md"))
        assert output_files, (
            f"no OUTPUT_*.md file written under {runtime_dir / 'output'} — "
            "SubagentAutoSendHook did not write the deliverable file"
        )
        output_file = output_files[0]
        assert output_file.name == "OUTPUT_1.md", (
            f"expected OUTPUT_1.md (first deliverable), got {output_file.name}"
        )
        file_content = output_file.read_text(encoding="utf-8")
        assert file_content == provider.DELIVERABLE_TEXT, (
            f"OUTPUT file content does not match subagent's final reply text: "
            f"got {file_content!r}, expected {provider.DELIVERABLE_TEXT!r}"
        )

        # --- assertion 4: parent notified via SubagentAutoSendHook ---
        # The hook fires on FINALLY_GRAPH, writes OUTPUT_<n>.md, then sends a
        # markdown notification to the PARENT's inbox via the bus. The
        # notification carries status, output path (via ResultMeta), and the
        # truncated (≤300 chars) deliverable text.
        parent_session_id = str(ctx.session)
        notif_deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < notif_deadline:
            if parent_session_id in await bus.sessions_with_pending():
                break
            await asyncio.sleep(0.05)
        assert parent_session_id in await bus.sessions_with_pending(), (
            "parent inbox was not notified — SubagentAutoSendHook did not fire "
            "(hook_runner wiring broken)"
        )
        notifications = await bus.consume(parent_session_id, limit=10)
        assert notifications, "parent inbox reported pending but consume returned empty"
        notif_content = str(notifications[0].payload)
        assert "Message from subagent" in notif_content, (
            f"notification missing 'Message from subagent' header: {notif_content[:200]}"
        )
        assert "status: success" in notif_content, (
            f"notification missing 'status: success': {notif_content[:200]}"
        )
        assert "Output:" in notif_content, (
            f"notification missing 'Output:' line (hook should carry output_path "
            f"via ResultMeta): {notif_content[:200]}"
        )
        assert provider.DELIVERABLE_TEXT in notif_content, (
            f"notification body does not contain the deliverable text: {notif_content[:200]}"
        )
    finally:
        await pool.shutdown_all()
        await broker.stop()
