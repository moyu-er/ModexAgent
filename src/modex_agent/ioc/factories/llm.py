"""LLM provider factory — creates provider from config.

Supports provider routing via ``interface_format``:
- ``openai_compatible`` → OpenAIProvider (our native OpenAI SDK provider)
- ``anthropic`` → LiteLLMProvider with ``anthropic/`` model prefix

Legacy ``openai/`` and ``anthropic/`` model-name prefixes are stripped before
routing; for Anthropic, the prefix is re-added before LiteLLM dispatch.
"""

from __future__ import annotations

import logging

from modex_agent.core.constants import InterfaceFormat
from modex_agent.core.llm_struct import LLMTimeoutPolicy, RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.safety import SafetyConfig

logger = logging.getLogger(__name__)

_OPENAI_PREFIX = "openai/"
_ANTHROPIC_PREFIX = "anthropic/"


def _strip_model_prefix(model: str) -> str:
    if not model:
        return model
    model = model.strip()
    if model.lower().startswith(_OPENAI_PREFIX):
        return model[len(_OPENAI_PREFIX) :]
    if model.lower().startswith(_ANTHROPIC_PREFIX):
        return model[len(_ANTHROPIC_PREFIX) :]
    return model


def create_llm_provider(
    config: LLMConfig,
    safety: SafetyConfig | None = None,
) -> LLMProvider:
    """Create an LLMProvider from config.

    Provider routing is driven by ``config.interface_format``.
    Any ``openai/`` or ``anthropic/`` prefix on ``config.model`` is stripped
    first; Anthropic format re-adds ``anthropic/`` before LiteLLM.

    Args:
        config: LLM configuration.
        safety: Optional safety policy configuration.

    Returns:
        Configured LLMProvider instance.
    """
    safety_policy: RuntimeSafetyPolicy | None = None
    if safety is not None:
        safety_policy = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(
                request_timeout_seconds=safety.llm.request_timeout,
                stream_idle_timeout_seconds=safety.llm.stream_idle_timeout,
                framework_max_retries=safety.llm.max_retries,
                retry_backoff_seconds=tuple(safety.llm.retry_backoff),
            ),
            turn=TurnTimeoutPolicy(
                agent_run_timeout_seconds=safety.turn.agent_run_timeout,
                hook_timeout_seconds=safety.turn.hook_timeout,
                tool_timeout_seconds=safety.turn.tool_timeout,
            ),
        )

    model = _strip_model_prefix(config.model)
    base_url = config.base_url.strip().rstrip("/") if config.base_url else None

    logger.info(
        "create_llm_provider: interface_format=%s model=%s base_url=%s",
        config.interface_format.value,
        model,
        base_url,
    )

    if config.interface_format == InterfaceFormat.ANTHROPIC:
        from modex_agent.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            model=f"{_ANTHROPIC_PREFIX}{model}",
            api_key=config.api_key or None,
            base_url=base_url,
            temperature=config.temperature,
            top_p=config.top_p,
            max_output_tokens=config.max_output_tokens,
            reasoning_effort=config.reasoning_effort,
            safety=safety_policy,
        )
    from modex_agent.providers.openai_provider import OpenAIProvider

    return OpenAIProvider(
        model=model,
        api_key=config.api_key or None,
        base_url=base_url,
        temperature=config.temperature,
        top_p=config.top_p,
        max_output_tokens=config.max_output_tokens,
        reasoning_effort=config.reasoning_effort,
        safety=safety_policy,
    )
