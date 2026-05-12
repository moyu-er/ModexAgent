"""LLM provider factory — creates provider from config."""

from __future__ import annotations

from framework.core.llm_error import LLMTimeoutPolicy, RuntimeSafetyPolicy, TurnTimeoutPolicy
from framework.core.provider import LLMProvider
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.safety import SafetyConfig


def create_llm_provider(
    config: LLMConfig,
    safety: SafetyConfig | None = None,
) -> LLMProvider:
    """Create an LLMProvider from config.

    Args:
        config: LLM configuration.
        safety: Optional safety policy configuration.

    Returns:
        Configured LLMProvider instance.
    """
    from framework.providers.litellm_provider import LiteLLMProvider

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

    return LiteLLMProvider(
        model=config.model,
        api_key=config.api_key or None,
        base_url=config.base_url or None,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        safety=safety_policy,
    )
