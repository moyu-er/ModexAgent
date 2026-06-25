"""LLM provider factory — creates provider from config.

Supports provider routing via model name prefix:
- ``openai/`` prefix → OpenAIProvider (prefix stripped from model name)
- no prefix (default) → LiteLLMProvider
"""

from __future__ import annotations

from modex_agent.core.llm_struct import LLMTimeoutPolicy, RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.safety import SafetyConfig

_OPENAI_PREFIX = "openai/"


def create_llm_provider(
    config: LLMConfig,
    safety: SafetyConfig | None = None,
) -> LLMProvider:
    """Create an LLMProvider from config.

    Provider routing:
    - Model name with ``openai/`` prefix → OpenAIProvider (prefix stripped)
    - Otherwise → LiteLLMProvider

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

    if config.model.startswith(_OPENAI_PREFIX):
        from modex_agent.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            model=config.model[len(_OPENAI_PREFIX) :],
            api_key=config.api_key or None,
            base_url=config.base_url or None,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            safety=safety_policy,
        )

    from modex_agent.providers.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(
        model=config.model,
        api_key=config.api_key or None,
        base_url=config.base_url or None,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        safety=safety_policy,
    )
