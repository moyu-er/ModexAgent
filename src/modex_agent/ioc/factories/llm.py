"""LLM provider factory — creates provider from config.

Routes every ``interface_format`` onto the single direct-HTTP provider
(ADR-0046): :class:`HTTPStreamProvider` with the matching protocol engine:

- ``openai_compatible`` → :class:`OpenAICompatProtocol` (chat completions wire)
- ``openai_response``   → :class:`OpenAIResponsesProtocol` (responses wire)
- ``anthropic``         → :class:`AnthropicProtocol` (messages wire)

This factory is the single ``InterfaceFormat`` branch point — the provider
itself carries zero format knowledge. Model names pass through VERBATIM
(user ruling 2026-08-26): no routing-prefix processing anywhere in the call
path — a stale ``openai/`` or ``anthropic/`` prefix simply reaches the API
as part of the model name. The factory also resolves the final request URL
(``endpoint_url`` verbatim when set, else the engine's ``url()`` join on
the normalized ``base_url``) and hands the provider one resolved ``url``.
The direct-HTTP ``providers/http/`` subsystem is the only provider
implementation (user ruling 2026-08-26: the legacy SDK providers are
removed).
"""

from __future__ import annotations

import logging

from modex_agent.core.constants import InterfaceFormat
from modex_agent.core.llm_struct import LLMTimeoutPolicy, RuntimeSafetyPolicy, TurnTimeoutPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.safety import SafetyConfig
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.protocol import LLMProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider

logger = logging.getLogger(__name__)


def create_llm_provider(
    config: LLMConfig,
    safety: SafetyConfig | None = None,
) -> LLMProvider:
    """Create an LLMProvider from config.

    All three ``interface_format`` values route to
    :class:`HTTPStreamProvider`, each wired with its protocol engine —
    the factory is the only ``InterfaceFormat`` branch point. ``config.model``
    passes through VERBATIM (no prefix stripping, no validation — a stale
    prefix reaches the API as part of the model name; user ruling
    2026-08-26). The request URL is resolved here: ``endpoint_url``
    verbatim when set, else the engine's ``url()`` join on the normalized
    ``base_url``. ``parse_think_tags`` stays at the engine default (True):
    ``LLMConfig`` has no such field, so the framework path always parses
    think tags.

    Args:
        config: LLM configuration.
        safety: Optional safety policy configuration.

    Returns:
        Configured ``HTTPStreamProvider`` instance.
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

    model = config.model
    base_url = config.base_url.strip().rstrip("/") if config.base_url else ""

    protocol: LLMProtocol
    match config.interface_format:
        case InterfaceFormat.OPENAI_COMPATIBLE:
            protocol = OpenAICompatProtocol()
        case InterfaceFormat.OPENAI_RESPONSE:
            protocol = OpenAIResponsesProtocol()
        case InterfaceFormat.ANTHROPIC:
            protocol = AnthropicProtocol()

    # endpoint_url (non-empty) is the complete URL used verbatim, bypassing
    # the engine's url() join (non-standard gateway override).
    url = config.endpoint_url if config.endpoint_url else protocol.url(base_url)

    logger.info(
        "create_llm_provider: interface_format=%s model=%s url=%s",
        config.interface_format.value,
        model,
        url,
    )

    return HTTPStreamProvider(
        model=model,
        api_key=config.api_key or None,
        url=url,
        protocol=protocol,
        temperature=config.temperature,
        top_p=config.top_p,
        max_output_tokens=config.max_output_tokens,
        reasoning_effort=config.reasoning_effort,
        headers=config.headers or None,
        responses_store=config.responses_store,
        safety=safety_policy,
    )
