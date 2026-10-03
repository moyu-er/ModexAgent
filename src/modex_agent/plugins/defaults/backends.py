"""Bundled service-level backend registrations — the ``in-memory`` pair.

The service-level counterparts of the compile-time default factories:
``register_default_backends`` registers the bundled in-memory
implementations under the name ``"in-memory"`` for both backend families
(message broker, control channel), so an unconfigured deployment
resolves exactly the classes it directly constructed before W2a.
"""

from __future__ import annotations

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.plugins.loader import PluginRegistrationContext

__all__ = ["IN_MEMORY_BACKEND_NAME", "register_default_backends"]

IN_MEMORY_BACKEND_NAME = "in-memory"
"""The bundled default backend name for both backend families (the
value ``AppConfig.broker_backend`` /
``AppConfig.control_channel_backend`` default to)."""


def register_default_backends(ctx: PluginRegistrationContext) -> None:
    """Register the ``in-memory`` broker + control-channel backends.

    The constructors take no config; the classes themselves are the
    factories (identity-stable, so re-registration is idempotent).
    """
    ctx.register_broker(IN_MEMORY_BACKEND_NAME, InMemoryMessageBroker)
    ctx.register_control_channel(IN_MEMORY_BACKEND_NAME, InMemoryControlChannel)
