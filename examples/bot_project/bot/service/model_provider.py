# bot/service/model_provider.py
"""Bot 模型 provider —— 池级 ContextVar 代理 + 显式 pin 两种薄壳。

框架 ReactLlmClient 只消费原生事件流（``provider.stream``，ADR-0046 单一事件
循环）。真实 provider 的构造/缓存/委托是两个 provider 的共享底座（模块级
helper，禁止复制第二份）：

- ``_cached_real_provider``：按 (provider.key, model) 构造并缓存
  ``synthesize_llm_config(resolved) → create_llm_provider`` 的产物；
- ``_stream_delegated``：Model-call 轨迹日志 + 请求信封重写（模型身份 +
  清空采样占位）+ 逐事件透传（``ToolCallDelta`` 等回调面没有通道的事件）。

- :class:`BotModelProvider` —— 池级单例，按当前 turn 的 ContextVar
  （``current_model_choice``）取 ResolvedModel 委托（D-5 缺省 = 继承调用方，
  契约化现状）。
- :class:`PinnedModelProvider` —— D-5 显式 pin：构造期解析固定模型，忽略
  ContextVar；由 :func:`resolve_agent_llm_pins` 在池装配期按 (provider.key,
  model) 缓存共享。

采样参数（temperature/top_p/max_output_tokens）刻意清空：真实 provider 在
构造期烘焙各自 model.yml 的参数，清空后由
``_with_sampling_defaults``/``build_body`` 回填。reasoning_effort v1 不透传
（留 TODO）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import Final

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import LLMStreamEvent, StreamFailure
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.multi_agent.materialize_deps import AgentLLMPin
from modex_agent.plugins.assembly.native_core import LlmDefaults
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.scope.spec import AgentSpec, ModelRef, PoolSpec

from .model_choice import current_model_choice
from .model_config import BotModelConfig, ResolvedModel

logger = logging.getLogger(__name__)

# Sentinel provider key used by model_config._placeholder_model_config().
# When the resolved model's provider key matches this, no real model is
# configured — the stream fails fast instead of making a doomed network call.
_PLACEHOLDER_PROVIDER_KEY: Final = "_unconfigured"

# Real-provider cache key: (provider.key, model.model) — the same identity
# BotModelProvider has always cached on.
ProviderCache = dict[tuple[str, str], LLMProvider]


def _cached_real_provider(
    model_config: BotModelConfig,
    cache: ProviderCache,
    resolved: ResolvedModel,
) -> LLMProvider:
    """按 (provider.key, model) 构造并缓存真实 provider —— 唯一构造点。

    两个代理 provider（BotModelProvider/PinnedModelProvider）共用：缓存
    dict 由调用方持有并可在多个 provider 实例间共享（同 (provider, model)
    复用同一个真实 provider 及其 httpx 连接）。
    """
    key = (resolved.provider.key, resolved.model.model)
    provider = cache.get(key)
    if provider is None:
        llm_cfg = model_config.synthesize_llm_config(resolved)
        provider = create_llm_provider(llm_cfg)
        cache[key] = provider
    return provider


async def _close_real_provider_cache(cache: ProviderCache) -> None:
    """Close every cached real provider and empty the cache.

    Each cached ``HTTPStreamProvider`` owns an ``httpx.AsyncClient`` —
    closing releases the connections. Legacy providers without ``aclose``
    are skipped. The cache is emptied so a post-close turn rebuilds fresh
    providers instead of reusing closed clients.
    """
    for provider in cache.values():
        if isinstance(provider, HTTPStreamProvider):
            await provider.aclose()
    cache.clear()


async def _stream_delegated(
    real: LLMProvider,
    resolved: ResolvedModel,
    request: LLMRequest,
) -> AsyncIterator[LLMStreamEvent]:
    """委托到真实 provider 的原生事件流 —— 唯一委托点。

    Model-call trajectory: the single chokepoint log that records which
    provider+model actually serves each turn. The resolved model owns
    identity + sampling; the framework-passed placeholders (default model
    name / descriptor temperature / max_output_tokens / top_p) never reach
    the wire — None falls back to the real provider's baked config in
    ``_with_sampling_defaults`` / ``build_body``. Events pass through
    verbatim — including ``ToolCallDelta``.
    """
    logger.info(
        "model call: provider=%s model=%s messages=%d",
        resolved.provider.name,
        resolved.model.model,
        len(request.messages),
    )
    delegated = request.model_copy(
        update={
            "model": resolved.model.model,
            "temperature": None,
            "top_p": None,
            "max_output_tokens": None,
        }
    )
    async for event in real.stream(delegated):
        yield event


def _provider_unavailable_failure(message: str) -> StreamFailure:
    return StreamFailure(
        error_info=LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message=message)
    )


class BotModelProvider(LLMProvider):
    """按 turn ContextVar 代理到真实 LLM provider（原生事件流委托）。"""

    def __init__(self, model_config: BotModelConfig) -> None:
        super().__init__()  # LLMProvider retry backoff config (chat()-path)
        self._model_config = model_config
        self._cache: ProviderCache = {}
        # ReactLlmClient 用 get_default_model() 构造 LLMStreamContext 与请求
        # 信封的 model 占位；真实 model 在 stream() 里按 ContextVar 重写。
        self.model = model_config.default_resolved().model.model

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        """Close every cached real provider and empty the cache.

        Called by ``BotService.stop()`` after workspaces are evicted (no
        in-flight turn still needs the providers).
        """
        await _close_real_provider_cache(self._cache)

    def _resolved(self) -> ResolvedModel:
        return current_model_choice.get() or self._model_config.default_resolved()

    def _real_provider(self, resolved: ResolvedModel) -> LLMProvider:
        return _cached_real_provider(self._model_config, self._cache, resolved)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Delegate to the ContextVar-resolved real provider's native event
        stream. Resolution/provider-build failures end the stream with one
        ``StreamFailure`` terminal event (the assembler folds it into an
        ERROR ``LLMResponse``, matching the legacy chat_stream fail-fast
        contract).
        """
        try:
            resolved = self._resolved()
        except Exception as exc:  # resolve failed: ERROR stream, don't raise
            logger.exception("BotModelProvider resolve failed")
            yield _provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        # Fail fast when no real model is configured (placeholder config from
        # model_config._placeholder_model_config). Avoids a doomed network call
        # to api.openai.com + the full retry/backoff loop before erroring.
        if resolved.provider.key == _PLACEHOLDER_PROVIDER_KEY:
            yield _provider_unavailable_failure(
                "no model configured — set one via WebUI Settings → Models or 'modexbot config'"
            )
            return
        try:
            real = self._real_provider(resolved)
        except Exception as exc:  # provider build failed: ERROR stream, don't raise
            logger.exception("BotModelProvider build failed")
            yield _provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        async for event in _stream_delegated(real, resolved, request):
            yield event


class PinnedModelProvider(LLMProvider):
    """显式 pin 的固定模型 provider（D-5）：构造期解析，忽略 ContextVar。

    池装配期由 :func:`resolve_agent_llm_pins` 按 (provider.key, model)
    缓存共享实例（同一模型被多个 agent pin 时复用一个 provider，共享同一
    真实 provider 缓存）。真实 provider 构造与 BotModelProvider 完全同源
    （``_cached_real_provider``）；委托走同一 ``_stream_delegated``。
    """

    def __init__(
        self,
        model_config: BotModelConfig,
        resolved: ResolvedModel,
        cache: ProviderCache | None = None,
    ) -> None:
        super().__init__()  # LLMProvider retry backoff config (chat()-path)
        self._model_config = model_config
        self._resolved = resolved
        # Shareable by construction: the resolver passes ONE cache dict for
        # every pinned provider of a pool (same-key real providers reuse the
        # same httpx client).
        self._cache: ProviderCache = cache if cache is not None else {}
        self.model = resolved.model.model

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        """Close the shared real-provider cache (see ``BotModelProvider``)."""
        await _close_real_provider_cache(self._cache)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Delegate to the pinned model's real provider — the per-turn
        ContextVar is deliberately ignored (that is the pin contract)."""
        try:
            real = _cached_real_provider(
                self._model_config, self._cache, self._resolved
            )
        except Exception as exc:  # provider build failed: ERROR stream, don't raise
            logger.exception("PinnedModelProvider build failed")
            yield _provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        async for event in _stream_delegated(real, self._resolved, request):
            yield event


def pinned_default_provider(
    slot_product: LLMProvider,
    model_config: BotModelConfig,
    resolved: ResolvedModel,
    cache: ProviderCache,
) -> LLMProvider:
    """默认模型 pin 的装配工厂(D-5 重定基 / D-6,唯一入口)。

    槽产物是 ``BotModelProvider`` 时包装为 ``PinnedModelProvider``(钉在
    默认模型,忽略 ContextVar);**自定义槽产物原样返回**——部署/测试通过
    组件槽替换 ``bot_default`` 时自持模型行为,默认 pin 不得绕过 LLM
    组件槽这个扩展点(装配边界类型收窄,先例见 pool/factory.py)。
    supply 侧 worker 与未配置子代理共用这一入口。
    """
    if isinstance(slot_product, BotModelProvider):
        return PinnedModelProvider(model_config, resolved, cache)
    return slot_product


def _nearest_declared_model(
    agent: AgentSpec,
    agents_by_name: Mapping[str, AgentSpec],
) -> ModelRef | None:
    """The nearest explicit ``model`` declaration up the declared parent chain.

    D-5 subtree default: a pinned agent's undeclared descendants inherit its
    pinned value ("最近显式声明生效"). The declared tree IS the spawn tree
    (subagents dispatch to their declared children), so a static walk at
    pool assembly is exact. ``None`` = no declaration anywhere on the chain
    (inherit the caller = the pool's per-turn selection).
    """
    seen: set[str] = set()
    current: AgentSpec | None = agent
    while current is not None and current.name not in seen:
        seen.add(current.name)
        if current.model is not None:
            return current.model
        parent = current.parent
        current = agents_by_name.get(parent) if parent is not None else None
    return None


def resolve_agent_llm_pins(
    pool_spec: PoolSpec,
    model_config: BotModelConfig,
    *,
    cache: ProviderCache | None = None,
) -> Mapping[str, AgentLLMPin]:
    """Resolve per-agent model pins (D-5) — the SINGLE assembly-layer
    resolution point for declared ``AgentSpec.model`` references.

    Runs once at pool assembly (``create_pool`` → ``AgentMaterializeDeps``);
    the framework template/materialize consumes the resolved values blind
    (architecture rule 5/9 — the framework never sees model.yml).

    ``cache`` lets the caller share ONE real-provider cache with other pins
    assembled at the same point (D-6: the supply-side reviewer pin) —
    same-key pins then share the underlying HTTP client across concerns.

    - Explicit declaration: ``model: {provider, name}`` → the agent's
      materialized product runs on a :class:`PinnedModelProvider` with the
      model's own profile (LlmDefaults + model_info, context_limit
      included).
    - Subtree default: an undeclared descendant of a pinned agent carries
      its nearest pinned ancestor's entry.
    - Sharing: agents pinning the same (provider.key, model) share one
      ``PinnedModelProvider`` and its real-provider cache.
    - Fail-fast (pool assembly aborts): unresolvable references; ``model``
      on the pool ROOT (the root follows the per-turn model selection —
      pin models on subagents); ``model`` on an external-strategy agent
      (external agents own their model config).

    Returns an empty mapping when the pool declares no pins (default path
    unchanged).
    """
    agents_by_name = {agent.name: agent for agent in pool_spec.agents}
    for agent in pool_spec.agents:
        if agent.model is None:
            continue
        if agent.is_root:
            raise ValueError(
                f"pool root {agent.name!r} cannot declare model "
                f"{agent.model.provider}/{agent.model.name!r}: the root follows "
                "the per-turn model selection — pin models on subagents"
            )
        # StrEnum: the str face of the declared reference equals the enum
        # member's value, so this is a value comparison against the enum.
        if strategy_name_of(agent.execution_strategy) == ExecutionStrategyKind.EXTERNAL:
            raise ValueError(
                f"external agent {agent.name!r} cannot declare model "
                f"{agent.model.provider}/{agent.model.name!r}: external agents "
                "own their model config"
            )

    pins: dict[str, AgentLLMPin] = {}
    pinned_providers: dict[tuple[str, str], PinnedModelProvider] = {}
    # One real-provider cache for every pin of this pool — same-key pins share
    # the underlying HTTP client. Caller-supplied caches additionally share
    # with the D-6 supply-side default-model pin (same assembly point).
    provider_cache: ProviderCache = cache if cache is not None else {}
    for agent in pool_spec.agents:
        if agent.is_root or (
            strategy_name_of(agent.execution_strategy)
            == ExecutionStrategyKind.EXTERNAL
        ):
            # Root: nothing to pin (template path never materializes it).
            # External: the external CLI owns its model config — never
            # materialized through the native path that consumes pins.
            continue
        ref = _nearest_declared_model(agent, agents_by_name)
        if ref is None:
            continue
        resolved = model_config.resolve(ref.provider, ref.name)
        if resolved is None:
            declaring = agent.name
            if agent.model is None:
                declaring = next(
                    ancestor
                    for ancestor in _chain_names(agent, agents_by_name)
                    if agents_by_name[ancestor].model is not None
                )
            raise ValueError(
                f"agent {agent.name!r} model reference "
                f"{ref.provider}/{ref.name!r} (declared on {declaring!r}) "
                "not found in model.yml — fix the declaration or add the "
                "model entry"
            )
        key = (resolved.provider.key, resolved.model.model)
        provider = pinned_providers.get(key)
        if provider is None:
            provider = PinnedModelProvider(model_config, resolved, provider_cache)
            pinned_providers[key] = provider
        pins[agent.name] = AgentLLMPin(
            provider=provider,
            defaults=LlmDefaults(
                model=resolved.model.model,
                temperature=resolved.model.temperature,
                max_output_tokens=resolved.model.max_output_tokens,
                reasoning_effort=resolved.model.reasoning_effort,
                model_info=resolved.model_info,
            ),
        )
    return MappingProxyType(pins)


def _chain_names(
    agent: AgentSpec,
    agents_by_name: Mapping[str, AgentSpec],
) -> list[str]:
    """Ancestor names from the agent's parent up to the root (nearest first)."""
    names: list[str] = []
    parent = agent.parent
    while parent is not None and parent in agents_by_name:
        names.append(parent)
        parent = agents_by_name[parent].parent
    return names
