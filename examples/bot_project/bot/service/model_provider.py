# bot/service/model_provider.py
"""BotModelProvider —— pool 级单例，按当前 turn 的 ContextVar 代理到真实 provider。

框架 ReactLlmClient 只消费原生事件流（``provider.stream``，ADR-0046 单一事件
循环）。本 provider 从 ContextVar 取 ResolvedModel，把请求信封的 model 重写为
解析结果后委托给按 (provider.key, model.model) 缓存的真实 provider——事件流
逐事件透传，不经回调折叠。折叠会丢失回调面没有通道的事件：``ToolCallDelta``
（工具参数流式增量，WebUI "Preparing" 心跳的信号源）在回调式 API 里无路可走，
曾导致 bot 实际回合中参数流式阶段前端完全静默。

采样参数（temperature/top_p/max_output_tokens）刻意清空：当前 turn 的模型及
参数由 current_model_choice ContextVar 决定（spec B1：ReactLlmClient 传的是
descriptor 占位值），真实 provider 在构造期烘焙各自 model.yml 的参数，清空后
由 ``_with_sampling_defaults``/``build_body`` 回填。
reasoning_effort v1 不透传（留 TODO）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import LLMStreamEvent, StreamFailure
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.providers.http.provider import HTTPStreamProvider

from .model_choice import current_model_choice
from .model_config import BotModelConfig, ResolvedModel

logger = logging.getLogger(__name__)

# Sentinel provider key used by model_config._placeholder_model_config().
# When the resolved model's provider key matches this, no real model is
# configured — the stream fails fast instead of making a doomed network call.
_PLACEHOLDER_PROVIDER_KEY = "_unconfigured"


class BotModelProvider(LLMProvider):
    """按 turn ContextVar 代理到真实 LLM provider（原生事件流委托）。"""

    def __init__(self, model_config: BotModelConfig) -> None:
        self._model_config = model_config
        self._cache: dict[tuple[str, str], LLMProvider] = {}
        # ReactLlmClient 用 get_default_model() 构造 LLMStreamContext 与请求
        # 信封的 model 占位；真实 model 在 stream() 里按 ContextVar 重写。
        self.model = model_config.default_resolved().model.model

    def get_default_model(self) -> str:
        return self.model

    async def aclose(self) -> None:
        """Close every cached real provider and empty the cache.

        Each cached ``HTTPStreamProvider`` owns an ``httpx.AsyncClient`` —
        closing releases the connections. Legacy providers without
        ``aclose`` are skipped. The cache is emptied so a post-close turn
        rebuilds fresh providers instead of reusing closed clients.
        Called by ``BotService.stop()`` after workspaces are evicted (no
        in-flight turn still needs the providers).
        """
        for provider in self._cache.values():
            if isinstance(provider, HTTPStreamProvider):
                await provider.aclose()
        self._cache.clear()

    def _resolved(self) -> ResolvedModel:
        return current_model_choice.get() or self._model_config.default_resolved()

    def _real_provider(self, resolved: ResolvedModel) -> LLMProvider:
        key = (resolved.provider.key, resolved.model.model)
        provider = self._cache.get(key)
        if provider is None:
            llm_cfg = self._model_config.synthesize_llm_config(resolved)
            provider = create_llm_provider(llm_cfg)
            self._cache[key] = provider
        return provider

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Delegate to the ContextVar-resolved real provider's native event
        stream, rewriting the envelope's model identity and clearing the
        framework's placeholder sampling params (the resolved provider owns
        them, baked at construction). Events pass through verbatim —
        including ``ToolCallDelta``.

        Resolution failures end the stream with one ``StreamFailure``
        terminal event (the assembler folds it into an ERROR ``LLMResponse``,
        matching the legacy chat_stream fail-fast contract).
        """
        try:
            resolved = self._resolved()
        except Exception as exc:  # resolve failed: ERROR stream, don't raise
            logger.exception("BotModelProvider resolve failed")
            yield self._provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        # Fail fast when no real model is configured (placeholder config from
        # model_config._placeholder_model_config). Avoids a doomed network call
        # to api.openai.com + the full retry/backoff loop before erroring.
        if resolved.provider.key == _PLACEHOLDER_PROVIDER_KEY:
            yield self._provider_unavailable_failure(
                "no model configured — set one via WebUI Settings → Models or 'modexbot config'"
            )
            return
        try:
            real = self._real_provider(resolved)
        except Exception as exc:  # provider build failed: ERROR stream, don't raise
            logger.exception("BotModelProvider build failed")
            yield self._provider_unavailable_failure(f"model provider unavailable: {exc}")
            return
        # Model-call trajectory: the single chokepoint log that records which
        # provider+model actually serves each turn (covers every real provider —
        # all protocol engines). INFO so it surfaces in normal operation.
        logger.info(
            "model call: provider=%s model=%s messages=%d",
            resolved.provider.name,
            resolved.model.model,
            len(request.messages),
        )
        # The resolved model owns identity + sampling; the framework-passed
        # placeholders (default model name / descriptor temperature /
        # max_output_tokens / top_p) never reach the wire — None falls back
        # to the real provider's baked config in _with_sampling_defaults /
        # build_body.
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

    @staticmethod
    def _provider_unavailable_failure(message: str) -> StreamFailure:
        return StreamFailure(
            error_info=LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message=message)
        )
