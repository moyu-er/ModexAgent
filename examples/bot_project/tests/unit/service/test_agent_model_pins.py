"""D-5 子代理模型 pin —— bot 装配层的解析与 PinnedModelProvider 行为。

- ``resolve_agent_llm_pins``:显式 pin 解析、子树默认(最近显式声明)、
  (provider.key, model) 共享实例、不可解析/根 pin/external pin fail-fast;
- ``PinnedModelProvider``:忽略当轮 ContextVar,委托信封携带 pinned 模型;
- 与 ``BotModelProvider`` 共享同一构造/委托底座(同一缓存 dict 时复用同一
  真实 provider 实例)。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import current_model_choice
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import (
    BotModelProvider,
    PinnedModelProvider,
    resolve_agent_llm_pins,
)

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.core.llm_request import LLMRequest, ReasoningEffort
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import Finish, TextDelta
from modex_agent.scope.spec import AgentSpec, ModelRef, PoolSpec

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - key: a
      name: "A"
      base_url: u
      interface_format: openai_compatible
      api_key: k
      models:
        - {name: M1, model: m1, temperature: 0.3, max_output_tokens: 1000}
        - {name: M2, model: m2, temperature: 0.9, max_output_tokens: 2000,
           context_limit: 65536, reasoning_effort: high}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


class _FakeReal(LLMProvider):
    """替身真实 provider:记录委托信封(模型身份的唯一载体)。"""

    def __init__(self) -> None:
        self.last_request: LLMRequest | None = None

    def get_default_model(self) -> str:
        return "fake"

    async def stream(self, request: LLMRequest) -> AsyncIterator[TextDelta | Finish]:
        self.last_request = request
        yield TextDelta(text="ok")
        yield Finish(finish_reason=FinishReason.STOP)


@pytest.fixture(autouse=True)
def _reset_ctxvar() -> Generator[None, None, None]:
    token = current_model_choice.set(None)
    yield
    current_model_choice.reset(token)


# ── resolve_agent_llm_pins ──────────────────────────────────────────────────


def test_no_declarations_yield_empty_map(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
    )
    assert dict(resolve_agent_llm_pins(pool, cfg)) == {}


def test_explicit_pin_resolves_provider_and_profile(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(
                name="sub",
                parent="root",
                model=ModelRef(provider="A", name="M2"),
            ),
        ],
    )
    pins = dict(resolve_agent_llm_pins(pool, cfg))
    assert set(pins) == {"sub"}
    pin = pins["sub"]
    assert isinstance(pin.provider, PinnedModelProvider)
    # LlmDefaults 取该模型档案(采样参数不复制到声明面,由模型条目独占)。
    assert pin.defaults.model == "m2"
    assert pin.defaults.temperature == 0.9
    assert pin.defaults.max_output_tokens == 2000
    assert pin.defaults.reasoning_effort == ReasoningEffort.HIGH
    assert pin.defaults.model_info is not None
    assert pin.defaults.model_info.model_name == "m2"
    assert pin.defaults.model_info.context_limit == 65536
    assert pin.defaults.model_info.max_output_tokens == 2000


def test_subtree_default_is_nearest_declaration(tmp_path: Path) -> None:
    """root 无声明、mid 显式 pin、leaf 无声明 → leaf 默认 = mid 的 pinned 值。"""
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(
                name="mid",
                parent="root",
                model=ModelRef(provider="A", name="M2"),
            ),
            AgentSpec(name="leaf", parent="mid"),
            AgentSpec(name="free", parent="root"),
        ],
    )
    pins = dict(resolve_agent_llm_pins(pool, cfg))
    assert set(pins) == {"mid", "leaf"}
    # 子树默认与声明者同源:同一 provider 实例、同一档案。
    assert pins["leaf"].provider is pins["mid"].provider
    assert pins["leaf"].defaults == pins["mid"].defaults
    # pinned 子树外的 agent 不受影响(缺省 = 继承调用方)。
    assert "free" not in pins


def test_same_model_pinned_by_two_agents_shares_provider(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(name="s1", parent="root", model=ModelRef(provider="A", name="M2")),
            AgentSpec(name="s2", parent="root", model=ModelRef(provider="A", name="M2")),
        ],
    )
    pins = dict(resolve_agent_llm_pins(pool, cfg))
    assert pins["s1"].provider is pins["s2"].provider


def test_unresolvable_reference_fails_fast_with_agent_and_ref(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(name="sub", parent="root", model=ModelRef(provider="A", name="NOPE")),
        ],
    )
    with pytest.raises(ValueError) as err:
        resolve_agent_llm_pins(pool, cfg)
    assert "'sub'" in str(err.value)
    assert "A/'NOPE'" in str(err.value)
    assert "not found in model.yml" in str(err.value)


def test_inherited_unresolvable_reference_names_declaring_agent(tmp_path: Path) -> None:
    """子先于父出现在扁平声明里时,报错带受影响 agent 与声明祖先。"""
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            # leaf 在 mid 之前(扁平声明允许);leaf 的有效引用继承自 mid。
            AgentSpec(name="leaf", parent="mid"),
            AgentSpec(name="mid", parent="root", model=ModelRef(provider="A", name="NOPE")),
        ],
    )
    with pytest.raises(ValueError) as err:
        resolve_agent_llm_pins(pool, cfg)
    assert "'leaf'" in str(err.value)
    assert "declared on 'mid'" in str(err.value)


def test_root_pin_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[AgentSpec(name="root", model=ModelRef(provider="A", name="M2"))],
    )
    with pytest.raises(ValueError, match="root 'root' cannot declare model"):
        resolve_agent_llm_pins(pool, cfg)


def test_external_agent_pin_rejected(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(
                name="ext",
                parent="root",
                execution_strategy=ExecutionStrategyKind.EXTERNAL,
                provider_kind=ProviderKind.OPENCODE,
                model=ModelRef(provider="A", name="M2"),
            ),
        ],
    )
    with pytest.raises(ValueError, match="external agent 'ext' cannot declare model"):
        resolve_agent_llm_pins(pool, cfg)


# ── PinnedModelProvider 行为 ────────────────────────────────────────────────


def _pinned_m1(cfg: BotModelConfig) -> PinnedModelProvider:
    resolved = cfg.resolve("A", "M1")
    assert resolved is not None
    return PinnedModelProvider(cfg, resolved)


def test_pinned_provider_ignores_contextvar(tmp_path: Path) -> None:
    """设置 current_model_choice 后 stream 仍用 pinned 模型(请求 model 字段)。"""
    cfg = _cfg(tmp_path)
    pinned = _pinned_m1(cfg)
    fake = _FakeReal()
    pinned._cache[("a", "m1")] = fake  # noqa: SLF001 — test seam: seeded real provider

    m2 = cfg.resolve("A", "M2")
    assert m2 is not None
    current_model_choice.set(m2)

    async def go() -> None:
        await pinned.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")]
        )

    asyncio.run(go())
    assert fake.last_request is not None
    assert fake.last_request.model == "m1"
    # 采样占位清空(真实 provider 构造期烘焙)。
    assert fake.last_request.temperature is None
    assert fake.last_request.max_output_tokens is None


def test_pinned_provider_get_default_model_is_pinned_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    pinned = _pinned_m1(cfg)
    assert pinned.get_default_model() == "m1"
    assert pinned.model == "m1"


def test_pinned_and_bot_providers_share_construction_base(tmp_path: Path) -> None:
    """收敛点:同一缓存 dict 时,PinnedModelProvider 与 BotModelProvider 复用
    同一真实 provider 实例(构造/缓存逻辑只有一份)。"""
    cfg = _cfg(tmp_path)
    resolved = cfg.resolve("A", "M1")
    assert resolved is not None
    bot = BotModelProvider(cfg)
    pinned = PinnedModelProvider(cfg, resolved, bot._cache)  # noqa: SLF001 — 同一缓存即同一底座

    real_from_bot = bot._real_provider(resolved)  # noqa: SLF001
    assert pinned._cache is bot._cache  # noqa: SLF001
    assert pinned._cache[("a", "m1")] is real_from_bot  # noqa: SLF001


def test_resolver_pins_share_one_real_provider_cache(tmp_path: Path) -> None:
    """同池所有 pin 共享一个真实 provider 缓存(不同模型 → 不同
    PinnedModelProvider,但同一缓存 dict)。"""
    cfg = _cfg(tmp_path)
    pool = PoolSpec(
        name="main",
        agents=[
            AgentSpec(name="root"),
            AgentSpec(name="s1", parent="root", model=ModelRef(provider="A", name="M1")),
            AgentSpec(name="s2", parent="root", model=ModelRef(provider="A", name="M2")),
        ],
    )
    pins = dict(resolve_agent_llm_pins(pool, cfg))
    assert pins["s1"].provider is not pins["s2"].provider
    assert pins["s1"].provider._cache is pins["s2"].provider._cache  # noqa: SLF001


# ── 真实装配缝:create_pool → AgentMaterializeDeps ──────────────────────────

_PINNED_DECLARATION = """\
pool:
  name: {pool_name}
  agents:
    main:
      description: pin assembly root
      agents:
        mid:
          model:
            provider: A
            name: M2
          agents:
            leaf:
        free:
"""

_PLAIN_DECLARATION = """\
pool:
  name: {pool_name}
  agents:
    main:
      description: plain root
      agents:
        sub:
"""


async def _create_pool(tmp_path: Path, declaration: str, cfg: BotModelConfig, pool_data=None):  # type: ignore[no-untyped-def]
    from unittest.mock import MagicMock

    from bot.service.model_choice import ModelChoiceRegistry
    from bot.service.pool import create_pool
    from bot.service.pool.declaration import declared_pool_build

    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.hook import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.messaging.broker_memory import InMemoryMessageBroker
    from modex_agent.multi_agent import SessionRetentionPolicy
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.registry import ComponentRegistry

    from ...declaration_driver import boot_from_yaml

    # DefaultPlugin registry — the ``subagents`` capability contributes the
    # derived ``task`` entries child-carrying agents need (V6).
    registry = ComponentRegistry()
    from modex_agent.plugins.loader import (
        ComponentRegistryLoader,
        PluginDiscoveryConfig,
    )

    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
    )

    pool_name = "pinned-pool"
    broker = InMemoryMessageBroker()
    await broker.start()
    instance = None
    try:
        boot = boot_from_yaml(
            declaration.format(pool_name=pool_name),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            registry=registry,
        )
        instance = await create_pool(
            pool_name=pool_name,
            declared=declared_pool_build(boot, pool_name),
            assembly_deps=PoolAssemblyDeps(),
            project_dir=tmp_path,
            data_dir=tmp_path / "data",
            workspace_registry=object(),
            workspace_resources=object(),
            broker=broker,
            output_adapter=MagicMock(spec=OutputAdapter),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=MagicMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            bot_model_config=cfg,
            model_choice_registry=ModelChoiceRegistry(),
            pool_data=pool_data,
        )
        return instance
    except BaseException:
        if instance is not None:
            await instance.pool.shutdown_all()
        raise
    finally:
        await broker.stop()


async def test_create_pool_threads_pins_into_materialize_deps(tmp_path: Path) -> None:
    """显式 pin(mid)+ 子树默认(leaf):装配产物 deps.agent_llm_pins 携带
    PinnedModelProvider 与该模型档案;pin 子树外的 free 缺省走默认模型
    pin(D-5 重定基,ADR-0050),池级默认字段保持 default_resolved。"""
    cfg = _cfg(tmp_path)
    instance = await _create_pool(tmp_path, _PINNED_DECLARATION, cfg)
    try:
        deps = instance.pool.materialize_deps
        assert deps is not None
        assert set(deps.agent_llm_pins) == {"mid", "leaf"}
        pin = deps.agent_llm_pins["mid"]
        assert isinstance(pin.provider, PinnedModelProvider)
        assert pin.defaults.model == "m2"
        assert pin.defaults.model_info is not None
        assert pin.defaults.model_info.context_limit == 65536
        # 子树默认 = 最近显式声明:leaf 与 mid 同源。
        assert deps.agent_llm_pins["leaf"].provider is pin.provider
        assert deps.agent_llm_pins["leaf"].defaults == pin.defaults
        # pin 子树外的 free(无声明)走缺省路径:默认模型 pin(非 ContextVar
        # 跟随的 BotModelProvider)。
        assert "free" not in deps.agent_llm_pins
        assert deps.llm_model == "m1"  # default_resolved
        assert isinstance(deps.llm_provider, PinnedModelProvider)
        assert deps.llm_provider.model == "m1"

        # 真实物化:pinned agent 的产物运行在 pin 的 provider 上,descriptor
        # 档案 = 该模型(含 context_limit)。
        template = instance.pool.get_template("mid")
        assert template is not None
        inst = await instance.pool.materialize_agent(
            "inv1.mid", template, parent_session_id="inv1.main"
        )
        assert inst.pipeline is not None
        react = inst.pipeline.agent
        assert isinstance(react, ReActAgent)
        assert react.provider is pin.provider
        assert inst.descriptor.llm_config.model == "m2"
        assert inst.descriptor.llm_config.model_info is not None
        assert inst.descriptor.llm_config.model_info.context_limit == 65536

        # 缺省 agent 的物化产物运行在默认模型 pin 上(同一实例),descriptor
        # 档案 = 池默认模型 —— 档案与实际调用一致(重定基后不再分叉)。
        free_template = instance.pool.get_template("free")
        assert free_template is not None
        free_inst = await instance.pool.materialize_agent(
            "inv1.free", free_template, parent_session_id="inv1.main"
        )
        assert free_inst.pipeline is not None
        free_react = free_inst.pipeline.agent
        assert isinstance(free_react, ReActAgent)
        assert free_react.provider is deps.llm_provider
        assert free_inst.descriptor.llm_config.model == "m1"
    finally:
        await instance.pool.shutdown_all()


async def test_create_pool_without_pins_keeps_default_deps(tmp_path: Path) -> None:
    """缺省(无声明)→ 无 pins,池默认 provider 为默认模型 pin
    (PinnedModelProvider 钉在 default_resolved;未配置子代理 = 默认模型,
    ADR-0050 D-5 重定基)。"""
    cfg = _cfg(tmp_path)
    instance = await _create_pool(tmp_path, _PLAIN_DECLARATION, cfg)
    try:
        deps = instance.pool.materialize_deps
        assert deps is not None
        assert dict(deps.agent_llm_pins) == {}
        assert isinstance(deps.llm_provider, PinnedModelProvider)
        assert deps.llm_provider.model == "m1"
        assert deps.llm_model == "m1"
        assert deps.llm_model_info is not None
        assert deps.llm_model_info.model_name == "m1"
    finally:
        await instance.pool.shutdown_all()


async def test_create_pool_fails_fast_on_unresolvable_pin(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    declaration = _PINNED_DECLARATION.replace("name: M2", "name: MISSING")
    with pytest.raises(ValueError) as err:
        await _create_pool(tmp_path, declaration, cfg)
    assert "'mid'" in str(err.value)
    assert "A/'MISSING'" in str(err.value)


# ── D-6 后台维护代理:experience reviewer = 默认模型 pin ─────────────────────

_EXPERIENCE_DECLARATION = """\
pool:
  name: {pool_name}
  agents:
    main:
      description: reviewer pin root
      capabilities:
        experience: {{}}
      agents:
        sub:
          model:
            provider: A
            name: M1
"""


async def _experience_pool(tmp_path: Path, cfg: BotModelConfig):  # type: ignore[no-untyped-def]
    """create_pool with the experience capability + a REAL pool_data (memory
    system) — the Stage 4 review-hook dispatch requires the memory system,
    so this is the real assembly face the pin must survive."""
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.scope.spec import AgentSpec
    from modex_agent.workspace.context import WorkspaceContext

    ws_ctx = WorkspaceContext.from_target(tmp_path, data_dir_name=".modex", home=tmp_path)
    pool_data = await build_pool_data(
        ws_ctx, "pinned-pool", AgentSpec(name="main"), None,
        PoolAssemblyDeps(memory=MemoryConfig()),
    )
    instance = await _create_pool(tmp_path, _EXPERIENCE_DECLARATION, cfg, pool_data)
    return instance, pool_data


async def _close_experience_pool(instance, pool_data) -> None:  # type: ignore[no-untyped-def]
    await instance.pool.shutdown_all()
    memory_system = pool_data.context_manager.memory_system
    if memory_system is not None:
        await memory_system.close()


async def test_experience_reviewer_provider_is_default_model_pin(tmp_path: Path) -> None:
    """SupplyInfra.default_llm_provider(→ capability supply → reviewer)是
    PinnedModelProvider(default_resolved),与本池声明 pin 共享同一真实
    provider 缓存;review hook 派发真实构建 reviewer(非 fail-soft 跳过)。"""
    cfg = _cfg(tmp_path)
    instance, pool_data = await _experience_pool(tmp_path, cfg)
    try:
        deps = instance.pool.materialize_deps
        assert deps is not None
        supply = deps.capability_supply.get("experience")
        assert supply is not None
        review_provider = supply.review_provider
        assert isinstance(review_provider, PinnedModelProvider)
        # pin 的对象 = model.yml 默认模型(M1/m1),不是任何声明引用。
        assert review_provider.model == "m1"
        # 与声明 pin(sub 也 pin M1)共享同一真实 provider 缓存 —— T5 缓存
        # 被 D-6 supply pin 复用,同 (provider, model) 只有一个真实 provider。
        sub_pin = deps.agent_llm_pins.get("sub")
        assert sub_pin is not None
        assert sub_pin.provider._cache is review_provider._cache  # noqa: SLF001 — test seam
        # 真实 reviewer(Stage 4 hook 派发产物)运行在该 pin 上
        # (ScopedFileAgent 将 provider 存为 _provider)。
        reviewer = supply.review_agent_for("main")
        assert reviewer is not None
        assert reviewer._provider is review_provider  # noqa: SLF001 — test seam
    finally:
        await _close_experience_pool(instance, pool_data)


async def test_experience_reviewer_ignores_turn_model_choice(tmp_path: Path) -> None:
    """turn 把 current_model_choice 设为非默认模型后触发 review,review 请求的
    model 仍是默认模型(经 PinnedModelProvider 委托断言)。"""
    cfg = _cfg(tmp_path)
    instance, pool_data = await _experience_pool(tmp_path, cfg)
    try:
        deps = instance.pool.materialize_deps
        assert deps is not None
        supply = deps.capability_supply.get("experience")
        assert supply is not None
        review_provider = supply.review_provider
        assert isinstance(review_provider, PinnedModelProvider)
        fake = _FakeReal()
        review_provider._cache[("a", "m1")] = fake  # noqa: SLF001 — test seam: seeded real provider

        m2 = cfg.resolve("A", "M2")
        assert m2 is not None
        current_model_choice.set(m2)

        await review_provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="review")]
        )
        assert fake.last_request is not None
        assert fake.last_request.model == "m1"
    finally:
        await _close_experience_pool(instance, pool_data)
