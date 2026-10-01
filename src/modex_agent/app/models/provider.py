"""Model providers — two thin shells: pool-level ContextVar proxy + explicit pin (promoted in W4b).

The framework ReactLlmClient consumes only the native event stream
(``provider.stream``, the ADR-0046 single event loop). Real-provider
construction/caching/delegation is the shared base of both providers
(module-level helpers; copying a second one is forbidden):

- ``_cached_real_provider``: constructs and caches, keyed by
  (provider.key, model), the product of
  ``synthesize_llm_config(resolved) → create_llm_provider``;
- ``_stream_delegated``: model-call trajectory log + request-envelope rewrite
  (model identity + cleared sampling placeholders) + verbatim per-event
  pass-through (for events like ``ToolCallDelta`` that have no channel on the
  callback face).

- :class:`ModelSelectionProvider` — the pool-level singleton; delegates to the
  ResolvedModel taken from the current turn's ContextVar
  (``current_model_choice``) (D-5 default = inherit the caller, the
  contractualized status quo).
- :class:`PinnedModelProvider` — the D-5 explicit pin: resolves the fixed
  model at construction time and ignores the ContextVar; instances are cached
  and shared by :func:`resolve_agent_llm_pins` at pool assembly, keyed by
  (provider.key, model).

Sampling parameters (temperature/top_p/max_output_tokens) are deliberately
cleared: the real provider bakes its own model.yml parameters at construction
time, and after clearing they are backfilled by
``_with_sampling_defaults``/``build_body``. reasoning_effort is not passed
through in v1 (TODO left).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import Final

from modex_agent.app.models.choice import current_model_choice
from modex_agent.app.models.registry import ModelRegistry, ResolvedModel
from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import LLMStreamEvent, StreamFailure
from modex_agent.multi_agent.materialize_deps import AgentLLMPin
from modex_agent.plugins.assembly.native_core import LlmDefaults
from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.scope.execution_kind import strategy_name_of
from modex_agent.scope.spec import AgentSpec, ModelRef, PoolSpec

logger = logging.getLogger(__name__)

# Sentinel provider key used by registry.placeholder_model_registry().
# When the resolved model's provider key matches this, no real model is
# configured — the stream fails fast instead of making a doomed network call.
_PLACEHOLDER_PROVIDER_KEY: Final = "_unconfigured"

# Real-provider cache key: (provider.key, model.model) — the same identity
# ModelSelectionProvider has always cached on.
ProviderCache = dict[tuple[str, str], LLMProvider]


def _cached_real_provider(
    model_config: ModelRegistry,
    cache: ProviderCache,
    resolved: ResolvedModel,
) -> LLMProvider:
    """Construct and cache the real provider keyed by (provider.key, model) — the single construction point.

    Shared by both proxy providers (ModelSelectionProvider/PinnedModelProvider):
    the cache dict is held by the caller and can be shared across provider
    instances (the same (provider, model) reuses one real provider and its
    httpx connections).
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
    """Delegate to the real provider's native event stream — the single delegation point.

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


class ModelSelectionProvider(LLMProvider):
    """Proxy to the real LLM provider by turn ContextVar (native event-stream delegation)."""

    def __init__(self, model_config: ModelRegistry) -> None:
        super().__init__()  # LLMProvider retry backoff config (chat()-path)
        self._model_config = model_config
        self._cache: ProviderCache = {}
        # ReactLlmClient builds LLMStreamContext and the request envelope's
        # model placeholder from get_default_model(); the real model is
        # rewritten per ContextVar inside stream().
        self.model = model_config.default_resolved().model.model

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        """Close every cached real provider and empty the cache.

        Called by the app service's ``stop()`` after workspaces are evicted (no
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
            logger.exception("ModelSelectionProvider resolve failed")
            yield _provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        # Fail fast when no real model is configured (placeholder config from
        # registry.placeholder_model_registry). Avoids a doomed network call
        # to api.openai.com + the full retry/backoff loop before erroring.
        if resolved.provider.key == _PLACEHOLDER_PROVIDER_KEY:
            yield _provider_unavailable_failure(
                "no model configured — set one via WebUI Settings → Models or 'modexbot config'"
            )
            return
        try:
            real = self._real_provider(resolved)
        except Exception as exc:  # provider build failed: ERROR stream, don't raise
            logger.exception("ModelSelectionProvider build failed")
            yield _provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        async for event in _stream_delegated(real, resolved, request):
            yield event


class PinnedModelProvider(LLMProvider):
    """Explicitly pinned fixed-model provider (D-5): resolved at construction, ignores the ContextVar.

    At pool assembly, :func:`resolve_agent_llm_pins` caches shared instances
    keyed by (provider.key, model) (when the same model is pinned by several
    agents, one provider is reused, sharing the same real-provider cache).
    Real-provider construction is fully co-sourced with ModelSelectionProvider
    (``_cached_real_provider``); delegation goes through the same
    ``_stream_delegated``.
    """

    def __init__(
        self,
        model_config: ModelRegistry,
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
        """Close the shared real-provider cache (see ``ModelSelectionProvider``)."""
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
    model_config: ModelRegistry,
    resolved: ResolvedModel,
    cache: ProviderCache,
) -> LLMProvider:
    """Assembly factory for the default-model pin (D-5 rebased / D-6, the single entry point).

    When the slot product is a ``ModelSelectionProvider`` it is wrapped as a
    ``PinnedModelProvider`` (pinned to the default model, ignoring the
    ContextVar); **a custom slot product is returned as-is** — deployments and
    tests that replace ``multi`` via the component slot own their model
    behavior, and the default pin must not bypass the LLM component slot as
    an extension point (assembly-boundary type narrowing; precedent in
    pool/factory.py). The supply-side worker and undeclared subagents share
    this entry point.
    """
    if isinstance(slot_product, ModelSelectionProvider):
        return PinnedModelProvider(model_config, resolved, cache)
    return slot_product


def _nearest_declared_model(
    agent: AgentSpec,
    agents_by_name: Mapping[str, AgentSpec],
) -> ModelRef | None:
    """The nearest explicit ``model`` declaration up the declared parent chain.

    D-5 subtree default: a pinned agent's undeclared descendants inherit its
    pinned value ("nearest explicit declaration wins"). The declared tree IS the spawn tree
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
    model_config: ModelRegistry,
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


def llm_defaults_of(resolved: ResolvedModel) -> LlmDefaults:
    """Map the resolved default model onto the framework value object."""
    model_info: ModelInfo = resolved.model_info
    return LlmDefaults(
        model=resolved.model.model,
        temperature=resolved.model.temperature,
        max_output_tokens=resolved.model.max_output_tokens,
        reasoning_effort=resolved.model.reasoning_effort,
        model_info=model_info,
    )
