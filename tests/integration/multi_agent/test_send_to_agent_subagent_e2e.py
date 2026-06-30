"""End-to-end联动: send_to_agent → subagent真实turn(mock LLM) → OUTPUT.md → parent通知.

这是一次关键联动的回归守护。它真实地走完整条多 agent 链路, 只把 LLM 换成脚本式 mock:

    main agent
      └─ SendToAgentTool / AgentCommunicationService.send_async
           └─ _create_dynamic_subagent
                ├─ 读 agents/helper.md 作为 subagent system prompt
                ├─ 注入 OutputMdProvider (output_base_dir 来自 workspace runtime_dir)
                ├─ DefaultAgentFactory 真实创建 ReActAgent + AgentPipeline
                │     (pipeline 经 bot 风格 wrap 挂上 workspace_manager + pool_name)
                ├─ SubagentAutoSendHook 接到真实 pipeline
                └─ broker 投递 task_request
      └─ AgentPool consumer 消费 → pipeline.process_message → 真实 react turn
           └─ mock LLM 从 *自己* 的 system prompt 里解析 OUTPUT.md 路径并写入
      └─ SubagentAutoSendHook.finally_turn 检测 OUTPUT.md → XML 通知 parent inbox

它专门锁定 ``pipeline._resolve_pool_data`` 的 subagent 守卫: 若该守卫被移除,
subagent 会改用 main agent 的 context_manager (``MAIN PROMPT``), 于是:
  - subagent 的 system prompt 变成 main 的;
  - 没有 OUTPUT.md 任务, mock LLM 抓不到路径, 文件不落盘;
  - parent 收到的通知 output_status=missing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry


# ---------------------------------------------------------------------------
# Fake workspace (mirrors bot layer's Workspace.pool_data shape)
# ---------------------------------------------------------------------------


class _FakePoolData:
    """Stands in for bot.service.workspace.PoolData.

    ``context_manager`` is deliberately the MAIN agent's (a sentinel prompt) so
    the test can detect whether the subagent pipeline wrongly adopted it.
    ``turn_store`` is a real store rooted at the workspace
    runtime dir — the subagent shares it (pool-level) so its AgentRuntime is
    constructed and FINALLY_TURN hooks fire.
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
# Scripted LLM provider — writes OUTPUT.md by reading its own system prompt
# ---------------------------------------------------------------------------


class _ScriptedProvider(StreamingLLMProvider):
    """Mock LLM that behaves like a well-behaved subagent.

    On the first call it parses the OUTPUT.md absolute path out of its own
    system prompt and emits a ``write`` tool call to that exact path. On the
    second call it finishes. Every call records the system prompt it saw so
    the test can assert which agent's prompt was actually used.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.seen_system_prompts: list[str] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        self.call_count += 1
        sys_msg = next((m for m in messages if m.get("role") == "system"), None)
        sys_text = str(sys_msg.get("content", "")) if sys_msg else ""
        self.seen_system_prompts.append(sys_text)

        if self.call_count == 1:
            m = re.search(r"([A-Za-z]:[^\s`]*OUTPUT\.md|/\S*OUTPUT\.md)", sys_text)
            if not m:
                return LLMResponse(content="I cannot find where to write output.")
            output_path = m.group(1)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={"path": output_path, "content": "# Deliverable\n\nDone."},
                        call_id="call_write_1",
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="done, see OUTPUT.md", finish_reason="stop")

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
) -> None:
    # --- project skeleton: agents/<type>.md + template yml ---
    project = tmp_path / "project"
    (project / "agents").mkdir(parents=True)
    (project / "agents" / "helper.md").write_text(
        "You are `helper`, a test subagent.\n\n"
        "## Communication Rules\n"
        "Write your deliverable to OUTPUT.md (path in the system prompt), "
        "then stop.\n",
        encoding="utf-8",
    )
    tpl_dir = project / "config" / "pools" / "main" / "templates"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "helper.yml").write_text(
        "agent_type: helper\n"
        "description: Test helper\n"
        "tool_preset: read_write\n"
        "max_steps: 5\n",
        encoding="utf-8",
    )
    template_registry = AgentTemplateRegistry(project)

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
    bus = LocalAgentMessageBus(producer=producer, consumer=consumer, broker=broker)

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
            instance.pipeline.workspace_manager = workspace_manager
            instance.pipeline.pool_name = "main"
        return instance

    factory.create_agent = _create_then_wire_workspace  # type: ignore[method-assign]

    session_registry = InMemorySessionRegistry()
    session_factory = SessionIdFactory()
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=bus,
        inbox_consumer=consumer,
        enable_inbox_polling=True,
        inbox_poll_interval=0.05,
        session_factory=session_factory,
        retention=SessionRetentionPolicy(),
        session_registry=session_registry,
    )

    # --- communication service for the main agent ---
    # The subagent inherits workspace + pool from the calling agent's runtime
    # context: workspace_manager (via the factory wrap above) and pool_name
    # are propagated to the subagent pipeline so it resolves its per-turn
    # stores from the same workspace pool_data the main agent uses.
    service = AgentCommunicationService(
        source=AgentAddress(name="main"),
        broker=broker,
        registry=pool,
        agent_bus=bus,
        comm_tracker=None,
        template_registry=template_registry,
        pool=pool,
        pool_name="main",
        project_dir=project,
        safety=None,
        inbox_consumer=consumer,
        main_agent_name="main",
        workspace_manager=workspace_manager,
        session_registry=session_registry,
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
            target_agent="helper",
            content="Produce the deliverable.",
            invocation_id="",
            context=ctx,
        )
        assert "Error" not in ack, f"send_async failed: {ack}"

        # Wait for the subagent turn to finish and notify the parent inbox.
        parent_session = str(ctx.session)
        deadline = asyncio.get_event_loop().time() + 10.0
        envelopes: list[Any] = []
        while asyncio.get_event_loop().time() < deadline:
            envelopes = await bus.poll(parent_session, limit=10)
            if envelopes:
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

        # --- assertion 2: OUTPUT.md task + workspace-rooted path in prompt ---
        assert "OUTPUT.md" in subagent_system
        injected_path = re.search(
            r"([A-Za-z]:[^\s`]*OUTPUT\.md|/\S*OUTPUT\.md)", subagent_system,
        )
        assert injected_path is not None, "no absolute OUTPUT.md path in prompt"
        output_file = Path(injected_path.group(1))
        assert output_file.is_relative_to(runtime_dir), (
            f"OUTPUT path {output_file} not under workspace runtime_dir {runtime_dir}"
        )

        # --- assertion 3: OUTPUT.md actually written ---
        assert output_file.exists(), f"OUTPUT.md was not written at {output_file}"
        assert "Deliverable" in output_file.read_text(encoding="utf-8")

        # --- assertion 4: parent received a normal, written notification ---
        assert envelopes, "parent inbox never received the subagent notification"
        content = envelopes[0].payload["content"]
        assert "<subagent_notification>" in content
        assert "<agent>helper</agent>" in content
        assert "<output_status>written</output_status>" in content, (
            f"expected output_status=written, got:\n{content}"
        )
        assert "<status>completed</status>" in content
        assert "<is_normal>true</is_normal>" in content
    finally:
        await pool.shutdown_all()
        await broker.stop()
