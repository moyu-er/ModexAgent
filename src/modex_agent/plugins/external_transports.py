"""External-transport registry — the ``provider_kind`` resolution face.

One process-level :class:`~modex_agent.core.backend_registry.BackendRegistry`
maps every external coding-agent ``provider_kind`` name onto the
transport constructor that builds it
(:class:`~modex_agent.agents.external.transports.abc.ExternalTransport`).
``ExternalExecutionStrategy._build_external_backend`` resolves the
declared kind through this registry — a new coding-agent transport is a
plugin registering one under a new kind name
(:meth:`~modex_agent.plugins.loader.PluginRegistrationContext.register_external_transport`),
zero framework edits; an unknown kind fails at the same assembly point
it always did, now listing the registered kinds.

The bundled ``opencode`` transport (:class:`OpenCodeTransport`) seeds
the registry at first access, so every resolution road works with or
without a plugin load; the seeding imports the transport module lazily
(the accessor stays import-light — importing this module must not pull
the aiohttp/CLI-harness stack in).
"""

from __future__ import annotations

from collections.abc import Callable

from modex_agent.agents.external.transports.abc import ExternalTransport
from modex_agent.core.backend_registry import BackendRegistry

__all__ = [
    "OPENCODE_TRANSPORT_KIND",
    "ExternalTransportFactory",
    "external_transport_registry",
]

OPENCODE_TRANSPORT_KIND = "opencode"
"""The bundled OpenCode CLI transport's provider-kind name."""

#: A transport constructor. Transports take no config (per-turn options
#: arrive on ``execute``); a transport that needs some closes over it at
#: its registration site — the callable signature stays
#: ``() -> ExternalTransport``.
ExternalTransportFactory = Callable[[], ExternalTransport]

_registry: BackendRegistry[ExternalTransport] | None = None


def external_transport_registry() -> BackendRegistry[ExternalTransport]:
    """The process-level external-transport registry — the resolution
    face ``ExternalExecutionStrategy._build_external_backend`` reads.

    Created on first access with the bundled ``opencode`` transport
    seeded, so every resolution road works with or without a plugin
    load; third-party registrations land here through the plugin loader
    (``PluginDiscoveryConfig.external_transports`` carries this
    registry).
    """
    global _registry
    if _registry is None:
        from modex_agent.agents.external.transports import OpenCodeTransport

        registry = BackendRegistry[ExternalTransport](family="external transport")
        registry.register(OPENCODE_TRANSPORT_KIND, OpenCodeTransport)
        _registry = registry
    return _registry
