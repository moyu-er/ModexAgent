"""Protocol-engine registry — the ``interface_format`` resolution face.

One process-level :class:`~modex_agent.core.backend_registry.BackendRegistry`
maps every ``interface_format`` name (``LLMConfig.interface_format``, a
plain string — the registry is the closed-set authority) onto the
protocol-engine constructor that builds it
(:class:`~modex_agent.providers.http.protocol.LLMProtocol`, ADR-0046).
``create_llm_provider`` resolves the configured name through this
registry — a fourth wire protocol is a plugin registering a new engine
under a new name
(:meth:`~modex_agent.plugins.loader.PluginRegistrationContext.register_protocol_engine`),
zero framework edits.

The three bundled wire protocols seed the registry at first access, so
every resolution road works with or without a plugin load; the seeding
imports the engine modules lazily (the accessor stays import-light —
importing this module must not pull the three engines in).
"""

from __future__ import annotations

from collections.abc import Callable

from modex_agent.core.backend_registry import BackendRegistry
from modex_agent.providers.http.protocol import LLMProtocol

__all__ = [
    "ANTHROPIC_FORMAT",
    "OPENAI_COMPATIBLE_FORMAT",
    "OPENAI_RESPONSE_FORMAT",
    "ProtocolEngineFactory",
    "protocol_engine_registry",
]

OPENAI_COMPATIBLE_FORMAT = "openai_compatible"
"""The bundled OpenAI Chat Completions compatible engine's name."""

OPENAI_RESPONSE_FORMAT = "openai_response"
"""The bundled OpenAI Responses API engine's name."""

ANTHROPIC_FORMAT = "anthropic"
"""The bundled Anthropic Messages API engine's name."""

#: A protocol-engine constructor. Engines take no config (the engine
#: instance is stateless — per-request state lives in the ``events``
#: closure); an engine that needs some closes over it at its
#: registration site — the callable signature stays ``() -> LLMProtocol``.
ProtocolEngineFactory = Callable[[], LLMProtocol]

_registry: BackendRegistry[LLMProtocol] | None = None


def protocol_engine_registry() -> BackendRegistry[LLMProtocol]:
    """The process-level protocol-engine registry — the resolution face
    :func:`~modex_agent.providers.factory.create_llm_provider` reads.

    Created on first access with the three bundled wire protocols
    seeded (``openai_compatible`` / ``openai_response`` / ``anthropic``),
    so every resolution road works with or without a plugin load;
    third-party registrations land here through the plugin loader
    (``PluginDiscoveryConfig.protocol_engines`` carries this registry).
    """
    global _registry
    if _registry is None:
        from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
        from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
        from modex_agent.providers.http.formats.openai_responses import (
            OpenAIResponsesProtocol,
        )

        registry = BackendRegistry[LLMProtocol](family="protocol engine")
        registry.register(OPENAI_COMPATIBLE_FORMAT, OpenAICompatProtocol)
        registry.register(OPENAI_RESPONSE_FORMAT, OpenAIResponsesProtocol)
        registry.register(ANTHROPIC_FORMAT, AnthropicProtocol)
        _registry = registry
    return _registry
