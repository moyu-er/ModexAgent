"""PA-03 tests — the ``session_title`` hook + background naming task.

Covers DESIGN §2.2–§2.5 contract through REAL plugin discovery, the REAL
declared-roster dispatch (``_dispatch_hooks``), and the REAL hook runner:

- plugin discovery registers the HOOK-slot factory
- roster dispatch wires the hook on a native main agent (applies_to)
- OutcomeFinallyHook semantics: suspend leg skipped; explicit
  ``StopReason.COMPLETED`` check; subagent sessions skipped
- the hook only enqueues a background task (returns immediately — main
  reply never waits for the model)
- existing title → zero model calls; concurrent turns → one task
- earliest real user input from the transcript, truncated to 1000 chars
- model output normalized (trim/single-line/length); empty/error → no save
- provider resolves lazily to the bot default-model PIN (D-6
  :class:`PinnedModelProvider`) — NOT the turn's ContextVar choice
- ClosableHook: ``aclose`` cancels pending tasks and closes the provider
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from bot.service.session_title import SessionTitleOps

from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import HookSpec
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.hook.runner import HookRunner
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.plugins.abc import AgentType, ComponentSlot
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    agent_context_chain,
)
from modex_agent.plugins.assembly.native_core import _dispatch_hooks
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.loader import (
    ComponentRegistry,
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.workspace.context import WorkspaceContext as WSIdentity
from modex_agent.workspace.paths import WorkspacePaths

from ._title_support import settle_titles, title_resources

_BOT_PROJECT_DIR = Path(__file__).resolve().parents[2]


async def _load_component_registry() -> ComponentRegistry:
    from modex_agent.plugins.defaults import DefaultPlugin

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(_BOT_PROJECT_DIR / "plugins",),
        ),
    )
    return registry


class _ScriptedProvider(LLMProvider):
    """Deterministic model seam: returns queued titles / errors, counts calls."""

    def __init__(self, responses: list[str | Exception]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []
        self.closed = False

    def get_default_model(self) -> str:
        return "scripted-title-model"

    async def stream(self, request: Any) -> Any:  # type: ignore[override]
        from modex_agent.core.llm_struct import FinishReason
        from modex_agent.core.stream_events import Finish, TextDelta

        self.calls.append(list(request.messages))
        item = self.responses.pop(0) if self.responses else ""
        if isinstance(item, Exception):
            raise item
        yield TextDelta(text=item)
        yield Finish(finish_reason=FinishReason.STOP)

    async def aclose(self) -> None:
        self.closed = True


class _ScriptedProviderSource:
    """Provider source seam: lazily builds the scripted provider once."""

    def __init__(self, provider: _ScriptedProvider) -> None:
        self._provider = provider
        self.built = 0

    def __call__(self) -> Any:
        self.built += 1
        return self._provider


def _spec(agent_name: str, *, hooks: tuple[str, ...] = ()) -> AssemblySpec:
    return AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name=agent_name,
        pool_name="default",
        tools=[],
        hooks=list(hooks),
        llm_provider="bot_default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=__import__(
            "modex_agent.plugins.assembly.spec", fromlist=["MemoryOverrides"]
        ).MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=WSIdentity(
            target=_BOT_PROJECT_DIR,
            paths=WorkspacePaths(root=_BOT_PROJECT_DIR / ".modex"),
            is_home=True,
        ),
    )


async def _make_ops(tmp_path: Path) -> tuple[SessionTitleOps, InMemorySessionRegistry]:
    store = LocalFileSessionStore(tmp_path / "session_index")
    registry = InMemorySessionRegistry(store=store)
    await registry.load_all()
    return SessionTitleOps(registry=registry), registry


def _main_session(session_id: str = "abc123.main") -> SessionInfo:
    return SessionInfo(
        session_id=session_id, agent_name="main", created_at=1, updated_at=2, metadata={}
    )


async def _make_hook(
    tmp_path: Path,
    provider: _ScriptedProvider,
    *,
    session: SessionInfo,
    user_input: Callable[[str], str | None] | None = None,
) -> tuple[Any, SessionTitleOps, InMemorySessionRegistry, _ScriptedProviderSource]:
    """Build the REAL hook through the REAL factory + naming-task owner.

    The provider source and transcript reader are the two deterministic
    test seams (network / transcript boundaries). Everything else —
    discovery, factory, hook, background task — is the real path.
    """
    ops, registry = await _make_ops(tmp_path)
    await registry.register(session)
    from bot.service.session_title_task import (
        SessionTitleNamingTask,
    )

    source = _ScriptedProviderSource(provider)
    naming = SessionTitleNamingTask(
        ops=ops,
        provider_source=source,
        transcript_reader=user_input or (lambda session_id: None),
    )
    from plugins.bot_hooks import SessionTitleHookFactory

    registry_components = await _load_component_registry()
    factory = SessionTitleHookFactory()
    spec = _spec(session.agent_name)
    base = AssemblyContext(
        registry=registry_components,
        workspace_ctx=spec.workspace_ctx,
        workspace_resources=title_resources(tmp_path, ops, naming),
    )
    chain = agent_context_chain(base, spec=spec, parent_session=None)
    hook = await factory.create(factory.config_model(), chain)  # type: ignore[arg-type]
    return hook, ops, registry, source


async def _dispatch_finally(hook: Any, ctx: Any, result: AgentResult | None) -> None:
    runner = HookRunner()

    runner.add(HookSpec(hook=hook))
    await runner.dispatch(
        HookPoint.FINALLY_GRAPH, ctx, HookPayload(data={"result": result})
    )


# ── plugin discovery + roster dispatch ──────────────────────────────────────


@pytest.mark.asyncio
async def test_plugin_discovery_registers_session_title_hook() -> None:
    registry = await _load_component_registry()
    factory = registry.resolve(ComponentSlot.HOOK, "session_title")
    # Dynamic plugin loading gives the discovered factory its own module
    # identity (PLAN §3.1) — verify by class name + declared dispatch
    # metadata, never isinstance.
    assert type(factory).__name__ == "SessionTitleHookFactory"
    assert factory.applies_to is not None
    assert AgentType.native_main in factory.applies_to
    assert AgentType.external_main in factory.applies_to
    assert factory.hook_runner is not None


@pytest.mark.asyncio
async def test_declared_roster_dispatch_wires_hook(tmp_path: Path) -> None:
    """A spec with hooks=['session_title'] gets the hook on its runner."""
    registry = await _load_component_registry()
    ops, _ = await _make_ops(tmp_path)
    from bot.service.session_title_task import SessionTitleNamingTask

    naming = SessionTitleNamingTask(
        ops=ops,
        provider_source=cast(Any, lambda: _ScriptedProvider(["t"])),
        transcript_reader=lambda session_id: None,
    )
    spec = _spec("main", hooks=("session_title",))
    base = AssemblyContext(
        registry=registry,
        workspace_ctx=spec.workspace_ctx,
        workspace_resources=title_resources(tmp_path, ops, naming),
    )
    chain = agent_context_chain(base, spec=spec)
    runner = HookRunner()
    await _dispatch_hooks(spec, registry, chain, runner, None)
    assert any("title" in s.hook.name.lower() for s in runner.hook_specs), (
        [s.hook.name for s in runner.hook_specs]
    )


# ── hook behavior ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completed_user_root_names_session_in_background(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["杭州旅行规划"])
    hook, ops, registry, source = await _make_hook(
        tmp_path,
        provider,
        session=session,
        user_input=lambda sid: "帮我规划杭州三天的行程",
    )
    ctx = _agent_context_for(session)
    start = time.monotonic()
    await _dispatch_finally(hook, ctx, AgentResult(stop_reason=StopReason.COMPLETED))
    elapsed = time.monotonic() - start
    assert elapsed < 0.5  # hook returned immediately; task dispatched async

    await settle_titles(hook)
    assert source.built == 1
    assert await ops.read_title("abc123.main") == "杭州旅行规划"
    assert len(provider.calls) == 1
    assert provider.calls[0][-1].role == MessageRole.USER


def _agent_context_for(session: SessionInfo) -> Any:
    """Minimal AgentContext for FINALLY_GRAPH dispatch (session + identity)."""
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.tools.manager import InMemoryToolManager

    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        identity=TurnIdentity(agent_id=session.agent_name, session=session, turn_id="t1"),
    )


@pytest.mark.asyncio
async def test_non_completed_outcomes_skipped(tmp_path: Path) -> None:
    provider = _ScriptedProvider(["不该出现"])
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=_main_session()
    )
    for reason in (StopReason.ERROR, StopReason.CANCELLED, StopReason.MAX_ITERATIONS):
        await _dispatch_finally(
            hook, _agent_context_for(_main_session()), AgentResult(stop_reason=reason)
        )
    # suspend leg (result=None) also skipped
    await _dispatch_finally(hook, _agent_context_for(_main_session()), None)
    await settle_titles(hook)
    assert source.built == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_subagent_sessions_skipped(tmp_path: Path) -> None:
    provider = _ScriptedProvider(["子代理不该命名"])
    child = SessionInfo(
        session_id="inv456.worker", agent_name="worker", parent_session_id="abc123.main"
    )
    hook, ops, registry, source = await _make_hook(tmp_path, provider, session=child)
    await _dispatch_finally(
        hook, _agent_context_for(child), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await settle_titles(hook)
    assert source.built == 0


@pytest.mark.asyncio
async def test_existing_title_zero_model_calls(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["不该调用"])
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=session
    )
    await registry.register(
        SessionInfo(
            session_id="abc123.main", agent_name="main", metadata={"title": "已有标题"}
        )
    )
    await _dispatch_finally(
        hook, _agent_context_for(session), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await settle_titles(hook)
    assert provider.calls == []
    assert await ops.read_title("abc123.main") == "已有标题"


@pytest.mark.asyncio
async def test_concurrent_turns_single_task(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["第一个"])
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=session, user_input=lambda sid: "内容"
    )
    ctx = _agent_context_for(session)
    await _dispatch_finally(hook, ctx, AgentResult(stop_reason=StopReason.COMPLETED))
    await _dispatch_finally(hook, ctx, AgentResult(stop_reason=StopReason.COMPLETED))
    await settle_titles(hook)
    assert len(provider.calls) == 1  # second dispatch found the pending task


@pytest.mark.asyncio
async def test_no_user_content_skips(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["无内容不该调用"])
    hook, ops, registry, source = await _make_hook(tmp_path, provider, session=session)
    await _dispatch_finally(
        hook, _agent_context_for(session), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await settle_titles(hook)
    assert provider.calls == []
    assert await ops.read_title("abc123.main") is None


@pytest.mark.asyncio
async def test_model_output_normalized_and_input_truncated(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["  \"标题\"\n解释  "])
    hook, ops, registry, source = await _make_hook(
        tmp_path,
        provider,
        session=session,
        user_input=lambda sid: "字" * 2500,
    )
    await _dispatch_finally(
        hook, _agent_context_for(session), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await settle_titles(hook)
    stored = await ops.read_title("abc123.main")
    assert stored is not None
    assert "\n" not in stored
    assert stored.strip() == stored
    # input truncated to 1000 chars before the model call
    user_msg = provider.calls[0][-1]
    content = str(user_msg.content)
    assert "字" * 1000 in content
    assert "字" * 1001 not in content


@pytest.mark.asyncio
async def test_model_error_no_save_retry_next_turn(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider([RuntimeError("boom"), "恢复后的标题"])
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=session, user_input=lambda sid: "内容"
    )
    ctx = _agent_context_for(session)
    await _dispatch_finally(hook, ctx, AgentResult(stop_reason=StopReason.COMPLETED))
    await settle_titles(hook)
    assert await ops.read_title("abc123.main") is None
    # failure removed the pending entry — the next completed turn retries
    await _dispatch_finally(hook, ctx, AgentResult(stop_reason=StopReason.COMPLETED))
    await settle_titles(hook)
    assert await ops.read_title("abc123.main") == "恢复后的标题"


@pytest.mark.asyncio
async def test_session_title_model_is_default_pin_ignoring_turn_choice(tmp_path: Path) -> None:
    """D-6:命名模型 = 默认模型 pin。生产 provider_source 构造的是
    PinnedModelProvider(default_resolved);触发轮选了非默认模型
    (current_model_choice),命名请求的 model 仍是默认模型。"""
    from bot.service.model_choice import current_model_choice
    from bot.service.model_config import BotModelConfig, ProviderCfg
    from bot.service.model_provider import PinnedModelProvider
    from bot.service.session_title_task import SessionTitleNamingTask

    from modex_agent.core.llm_request import LLMRequest
    from modex_agent.core.llm_struct import FinishReason
    from modex_agent.core.stream_events import Finish, TextDelta

    cfg = BotModelConfig(
        default_provider="A",
        default_model="M1",
        providers=[
            ProviderCfg(
                key="a",
                name="A",
                base_url="u",
                api_key="k",
                models=[
                    {"name": "M1", "model": "m1"},
                    {"name": "M2", "model": "m2"},
                ],
            ),
        ],
    )

    class _RecordingReal(LLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.models: list[str] = []

        def get_default_model(self) -> str:
            return "m1"

        async def stream(self, request: LLMRequest) -> Any:  # type: ignore[override]
            self.models.append(request.model)
            yield TextDelta(text="默认模型的标题")
            yield Finish(finish_reason=FinishReason.STOP)

    real = _RecordingReal()
    # 生产 seam 形状:provider_source() → PinnedModelProvider(默认模型)。
    pinned = PinnedModelProvider(cfg, cfg.default_resolved())
    pinned._cache[("a", "m1")] = real  # noqa: SLF001 — test seam: seeded real provider

    ops, registry = await _make_ops(tmp_path)
    session = _main_session()
    await registry.register(session)
    naming = SessionTitleNamingTask(
        ops=ops,
        provider_source=lambda: pinned,
        transcript_reader=lambda sid: "内容",
    )
    # 触发轮选了非默认模型 M2 —— 命名不得继承。
    m2 = cfg.resolve("A", "M2")
    assert m2 is not None
    token = current_model_choice.set(m2)
    try:
        naming.submit("default", session)
        await settle_titles(naming)
    finally:
        current_model_choice.reset(token)
    assert real.models == ["m1"]
    assert await ops.read_title("abc123.main") == "默认模型的标题"


@pytest.mark.asyncio
async def test_hook_aclose_cancels_only_own_pool_workspace_aclose_full(tmp_path: Path) -> None:
    """Pool-stop (hook.aclose) cancels its own tasks but NOT the shared
    provider; workspace teardown (naming.aclose) cancels all + closes."""
    session = _main_session()
    provider = _ScriptedProvider(["迟到的标题", "另一池标题"])
    release = asyncio.Event()
    original_stream = provider.stream

    async def _slow_stream(request: Any) -> Any:
        await release.wait()
        async for event in original_stream(request):
            yield event

    provider.stream = _slow_stream  # type: ignore[method-assign]
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=session, user_input=lambda sid: "内容"
    )
    naming = hook._naming
    # a second pool's task on the same workspace-shared owner
    other_session = SessionInfo(session_id="zzz999.coder", agent_name="coder")
    await registry.register(other_session)
    naming.submit("coder", other_session)

    await _dispatch_finally(
        hook, _agent_context_for(session), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await asyncio.sleep(0.05)  # let both tasks park on the gate
    # pool stop: only the hook's pool tasks die; provider stays open and
    # the OTHER pool's task keeps running
    await hook.aclose()
    assert provider.closed is False
    release.set()
    await asyncio.sleep(0.05)
    assert await ops.read_title("abc123.main") is None
    # the other pool's task completes: it consumed the queued response
    assert await ops.read_title("zzz999.coder") == "迟到的标题"
    # workspace teardown: provider released
    await naming.aclose()
    assert provider.closed is True


@pytest.mark.asyncio
async def test_manual_rename_wins_over_late_auto(tmp_path: Path) -> None:
    session = _main_session()
    provider = _ScriptedProvider(["自动标题"])
    gate = asyncio.Event()
    original_stream = provider.stream

    async def _gated_stream(request: Any) -> Any:
        await gate.wait()
        async for event in original_stream(request):
            yield event

    provider.stream = _gated_stream  # type: ignore[method-assign]
    hook, ops, registry, source = await _make_hook(
        tmp_path, provider, session=session, user_input=lambda sid: "内容"
    )
    await _dispatch_finally(
        hook, _agent_context_for(session), AgentResult(stop_reason=StopReason.COMPLETED)
    )
    await asyncio.sleep(0.02)  # let the task park on the gate
    await ops.set_title("abc123.main", "人工标题")
    gate.set()
    await settle_titles(hook)
    assert await ops.read_title("abc123.main") == "人工标题"
