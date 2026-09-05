from __future__ import annotations

import asyncio  # asyncio-native pool runtime and emitter contract (anyio not required)
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import boot_scope_declaration, declared_pool_build
from bot.workspace.pool_data import PoolData, build_pool_data
from plugins.bot_strategies import BotDefaultLLMConfig

from modex_agent.adapters.output import NullOutputAdapter
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.llm_struct import FinishReason, LLMResponse, RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage, ToolCall
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.presets import main_agent_memory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.models import JsonValue
from modex_agent.trace.store import JsonlSpanQuery
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_BOT_PROJECT = Path(__file__).resolve().parents[3]
_ROOT_SESSION = SessionInfo.from_str("harbor.orchestrator")


class _ScriptedProvider(CallbackStreamProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(retry_backoff_seconds=())
        self._responses = deque(responses)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        return self._responses.popleft()

    def get_default_model(self) -> str:
        return "openai/harbor-scripted"


class _DelegatingProvider(CallbackStreamProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = model, temperature, max_output_tokens, tools, kwargs
        if any(message.tool_calls for message in messages):
            return LLMResponse(content="delegated pool answer", finish_reason=FinishReason.STOP)
        content = "\n".join(str(message.content or "") for message in messages)
        if "Return child answer." in content:
            return LLMResponse(content="child pool answer", finish_reason=FinishReason.STOP)
        return LLMResponse(
            content=None,
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    tool_name="task",
                    arguments={"target_agent": "explore", "content": "Return child answer."},
                    call_id="delegate-1",
                )
            ],
        )

    def get_default_model(self) -> str:
        return "openai/harbor-delegating"


class _FutureEmitter(ContentEmitter[ReActEvent]):
    def __init__(self, completion: asyncio.Future[AgentResult]) -> None:
        super().__init__()
        self._completion = completion

    async def emit_delta(self, delta: str) -> None:
        _ = delta

    async def emit_complete(self, result: AgentResult) -> None:
        self._completion.set_result(result)

    async def emit_error(self, error: str) -> None:
        if not self._completion.done():
            self._completion.set_result(AgentResult(error=error))


async def _scripted_registry(provider: LLMProvider) -> ComponentRegistry:
    """Production-shaped registry with the scripted provider behind ``bot_default``."""
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT / "plugins",),
        ),
    )
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        "bot_default",
        SimpleFactory(provider, BotDefaultLLMConfig),
        overwrite=True,
    )
    return registry


def _compile_registry() -> ComponentRegistry:
    """DefaultPlugin-only registry for the compile step (the shipped
    declaration's ``capabilities:`` blocks resolve against it)."""
    from modex_agent.plugins.loader import PluginRegistrationContext

    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


async def _create_scripted_pool(
    provider: LLMProvider,
    completions: dict[str, asyncio.Future[AgentResult]],
    data_dir: Path,
) -> tuple[
    PoolInstance,
    InMemoryMessageBroker,
    PoolData,
    asyncio.Future[tuple[str, str]],
    asyncio.Event,
]:
    boot = boot_scope_declaration(
        declaration_path=_BOT_PROJECT / "config" / "scopes" / "bot.yml",
        project_dir=_BOT_PROJECT,
        data_dir=data_dir,
        graphs_dirs=(),
        default_llm_provider="bot_default",
        registry=_compile_registry(),
    )
    declared = declared_pool_build(boot, "coder")
    assembly_deps = PoolAssemblyDeps(memory=main_agent_memory())
    pool_data = await build_pool_data(
        WorkspaceContext(
            target=_BOT_PROJECT,
            paths=WorkspacePaths(root=data_dir),
            is_home=False,
        ),
        "coder",
        declared.pool.root_agent,
        provider,
        assembly_deps,
    )
    broker = InMemoryMessageBroker()
    await broker.start()
    child_created: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
    child_emitter_ready = asyncio.Event()

    def emitter_factory(session_id: str, pool_name: str) -> _FutureEmitter:
        _ = pool_name
        completion = completions.setdefault(
            session_id,
            asyncio.get_running_loop().create_future(),
        )
        if session_id != _ROOT_SESSION.session_id:
            child_emitter_ready.set()
        return _FutureEmitter(completion)

    async def on_subagent_created(child_id: str, parent_id: str, pool_name: str) -> None:
        _ = pool_name
        if not child_created.done():
            child_created.set_result((child_id, parent_id))

    pool_instance = await create_pool(
        pool_name="coder",
        declared=declared,
        assembly_deps=assembly_deps,
        project_dir=_BOT_PROJECT,
        data_dir=data_dir,
        broker=broker,
        output_adapter=NullOutputAdapter(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        im_ui=NullOutputAdapter(),
        shared_hooks=[],
        shared_hook_runner=HookRunner(),
        shared_interceptor_chain=InterceptorChain(),
        pool_data=pool_data,
        emitter_factory=emitter_factory,
        on_subagent_created=on_subagent_created,
        bot_model_config=None,
        model_choice_registry=ModelChoiceRegistry(),
        workspace_registry=object(),
        workspace_resources=object(),
        component_registry=await _scripted_registry(provider),
    )
    return pool_instance, broker, pool_data, child_created, child_emitter_ready


@pytest.mark.asyncio
async def test_real_pool_drives_direct_answer_to_emitter_completion(tmp_path: Path) -> None:
    completion: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
    completions = {_ROOT_SESSION.session_id: completion}
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content="direct pool answer",
                finish_reason=FinishReason.STOP,
            ),
        ]
    )
    pool_instance, broker, pool_data, _, _ = await _create_scripted_pool(
        provider,
        completions,
        tmp_path,
    )
    poller = pool_instance.pool._poller
    assert poller is not None
    poller_task = poller._task
    assert poller_task is not None

    try:
        await pool_instance.pool.submit_input(
            _ROOT_SESSION.session_id,
            InputMessage(
                session=_ROOT_SESSION,
                content="Answer directly.",
            ),
        )
        result = await asyncio.wait_for(completion, timeout=5)
    finally:
        await pool_instance.pool.shutdown_all()
        await broker.stop()
        memory_system = pool_data.context_manager.memory_system
        assert memory_system is not None
        await memory_system.close()

    assert result.content == "direct pool answer"
    assert poller_task.done()
    assert poller._inflight == {}
    # The tracing capability (FILE backend — the boot fallback's default)
    # persists the turn's spans even under a custom emitter factory: the
    # retired trace gap (emitter-carried pools lost every span) is closed.
    spans = await JsonlSpanQuery(tmp_path / "runtime_state" / "coder" / "trace").list_by_session(
        _ROOT_SESSION.session_id
    )
    assert {span.name for span in spans} == {"chat", "invoke_agent"}


@pytest.mark.asyncio
async def test_real_pool_drives_delegated_subagent_and_persists_trace(
    tmp_path: Path,
) -> None:
    completion: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
    completions = {_ROOT_SESSION.session_id: completion}
    (
        pool_instance,
        broker,
        pool_data,
        child_created,
        child_emitter_ready,
    ) = await _create_scripted_pool(
        _DelegatingProvider(),
        completions,
        tmp_path,
    )

    try:
        await pool_instance.pool.submit_input(
            _ROOT_SESSION.session_id,
            InputMessage(session=_ROOT_SESSION, content="Delegate this task."),
        )
        child_id, parent_id = await asyncio.wait_for(child_created, timeout=5)
        await asyncio.wait_for(child_emitter_ready.wait(), timeout=5)
        child_result = await asyncio.wait_for(completions[child_id], timeout=5)
        parent_result = await asyncio.wait_for(completion, timeout=5)
    finally:
        await pool_instance.pool.shutdown_all()
        await broker.stop()
        memory_system = pool_data.context_manager.memory_system
        assert memory_system is not None
        await memory_system.close()

    assert parent_id == _ROOT_SESSION.session_id
    assert child_result.content == "child pool answer"
    assert parent_result.content == "delegated pool answer"
    # Trace persistence covers the delegation too: the orchestrator's
    # handoff chain and the subagent's own turn both reach the pool's
    # span store under their session ids.
    query = JsonlSpanQuery(tmp_path / "runtime_state" / "coder" / "trace")
    root_spans = await query.list_by_session(_ROOT_SESSION.session_id)
    child_spans = await query.list_by_session(child_id)
    assert {span.name for span in root_spans} == {
        "chat",
        "execute_tool_batch",
        "execute_tool",
        "agent.handoff",
        "invoke_agent",
    }
    assert {span.name for span in child_spans} == {"chat", "invoke_agent"}
