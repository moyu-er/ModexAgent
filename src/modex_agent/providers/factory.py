"""LLM provider factory — creates provider from config (W3a: moved from the
deleted ``ioc/factories/llm.py``; providers is the owning domain — the
factory consumes ``LLMConfig``/``SafetyConfig`` and produces the one
direct-HTTP provider implementation).

Routes every ``interface_format`` name onto the single direct-HTTP
provider (ADR-0046): :class:`HTTPStreamProvider` with the protocol engine
the name resolves to in the process-level registry
(:func:`modex_agent.providers.protocol_engines.protocol_engine_registry`)
— the bundled ``openai_compatible`` / ``openai_response`` / ``anthropic``
engines plus whatever a plugin registered under a new name. An unknown
name raises the registry's loud error listing the registered names.

The factory carries zero per-format knowledge beyond that resolve — the
provider itself carries none at all. Model names pass through VERBATIM
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

from modex_agent.core.llm_struct import (
    DeadlinePolicy,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.provider import LLMProvider
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.providers.llm_config import LLMConfig
from modex_agent.providers.protocol_engines import protocol_engine_registry
from modex_agent.providers.safety_config import SafetyConfig

logger = logging.getLogger(__name__)


def create_llm_provider(
    config: LLMConfig,
    safety: SafetyConfig | None = None,
) -> LLMProvider:
    """Create an LLMProvider from config.

    Every ``interface_format`` name routes to
    :class:`HTTPStreamProvider`, wired with the engine the name resolves
    to in the protocol-engine registry (an unknown name raises the
    registry's error listing the registered names).
    ``config.model`` passes through VERBATIM (no prefix stripping, no
    validation — a stale prefix reaches the API as part of the model
    name; user ruling 2026-08-26). The request URL is resolved here:
    ``endpoint_url`` verbatim when set, else the engine's ``url()`` join
    on the normalized ``base_url``. ``parse_think_tags`` stays at the
    engine default (True): ``LLMConfig`` has no such field, so the
    framework path always parses think tags.

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
                hook_timeout_seconds=safety.turn.hook_timeout,
                tool_timeout_seconds=safety.turn.tool_timeout,
            ),
            deadline=DeadlinePolicy(
                chunk_renew_seconds=safety.deadline.chunk_renew_seconds,
                max_ahead_seconds=safety.deadline.max_ahead_seconds,
                watchdog_poll_seconds=safety.deadline.watchdog_poll_seconds,
            ),
        )

    model = config.model
    base_url = config.base_url.strip().rstrip("/") if config.base_url else ""

    protocol = protocol_engine_registry().resolve(config.interface_format)

    # endpoint_url (non-empty) is the complete URL used verbatim, bypassing
    # the engine's url() join (non-standard gateway override).
    url = config.endpoint_url if config.endpoint_url else protocol.url(base_url)

    logger.info(
        "create_llm_provider: interface_format=%s model=%s url=%s",
        config.interface_format,
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
