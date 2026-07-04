# bot/service/model_provider.py
"""BotModelProvider —— pool 级单例，按当前 turn 的 ContextVar 代理到真实 provider。

框架 ReactLlmClient 调用 provider 时不传 model、但传 temperature/max_output_tokens（来自
descriptor）。本 provider 从 ContextVar 取 ResolvedModel，覆盖 model/temperature/max_output_tokens
后转发给按 (provider.key, model.model) 缓存的真实 provider。
reasoning_effort v1 不透传（留 TODO）。
"""

from __future__ import annotations

import logging
from typing import Any

from modex_agent.core.constants import FinishReason
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.types import LLMResponse
from modex_agent.ioc.factories.llm import create_llm_provider

from .model_choice import current_model_choice
from .model_config import BotModelConfig, ResolvedModel

logger = logging.getLogger(__name__)


class BotModelProvider(StreamingLLMProvider):
    """按 turn ContextVar 代理到真实 LLM provider。"""

    def __init__(self, model_config: BotModelConfig) -> None:
        super().__init__()
        self._model_config = model_config
        self._cache: dict[tuple[str, str], LLMProvider] = {}
        # ReactLlmClient 用 getattr(provider, "model", None) 构造 LLMStreamContext。
        self.model = model_config.default_resolved().model.model

    def get_default_model(self) -> str:
        return self.model

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

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        # model/temperature/max_output_tokens 来自框架 ABC 签名，此处故意忽略——
        # 当前 turn 的模型及其参数由 current_model_choice ContextVar 决定（spec B1：
        # ReactLlmClient 不传 model，descriptor 的 temp/max_output_tokens 是冗余占位）。
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Any = None,  # noqa: ANN401  matches StreamingLLMProvider ABC
        on_reasoning_delta: Any = None,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> LLMResponse:
        try:
            resolved = self._resolved()
            real = self._real_provider(resolved)
        except Exception as exc:  # provider 创建失败：返回 ERROR，不抛
            logger.exception("BotModelProvider resolve/build failed")
            return LLMResponse(
                content=None,
                finish_reason=FinishReason.ERROR.value,
                error=f"model provider unavailable: {exc}",
            )
        # Model-call trajectory: the single chokepoint log that records which
        # provider+model actually serves each turn (covers every real provider —
        # OpenAI, LiteLLM, …). INFO so it surfaces in normal operation.
        logger.info(
            "model call: provider=%s model=%s messages=%d",
            resolved.provider.name,
            resolved.model.model,
            len(messages),
        )
        # NOTE: model/temperature/max_output_tokens are NOT forwarded. The real
        # provider is constructed per resolved model via create_llm_provider
        # (see _real_provider), which bakes in the ROUTING-STRIPPED model (e.g.
        # "openai/step-3.7-flash" -> OpenAIProvider(model="step-3.7-flash")) plus
        # the model's own temperature/max_output_tokens. Forwarding model= here
        # would re-inject the routing prefix and the API would reject it ("model
        # not found"). Let the baked provider own these values.
        return await real.chat_stream(
            messages=messages,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
            # TODO: reasoning_effort v1 未透传给真实 provider
        )
