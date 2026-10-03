"""BackendRegistry — name → factory for one service-level backend family.

The service-level counterpart of the compile-time component slots (same
role as :class:`~modex_agent.plugins.loader.ChannelAdapterRegistry`,
generic over the backend ABC): infrastructure backends such as the
message broker and the control channel resolve once per boot from
config, never through scope compilation. Plugins contribute named
backends through
:meth:`~modex_agent.plugins.loader.PluginRegistrationContext.register_broker`
/ ``register_control_channel``; the bundled defaults
(:mod:`modex_agent.plugins.defaults.backends`) register the
``in-memory`` pair.

Registries are process-singletons created at boot (the app-service
registry load) — one instance per backend family.
"""

from __future__ import annotations

from collections.abc import Callable

from modex_agent.control.channel import ControlChannel
from modex_agent.messaging.broker import MessageBroker

__all__ = [
    "BackendRegistry",
    "BrokerFactory",
    "ControlChannelFactory",
]

#: A message-broker backend constructor. Backends take no config today;
#: a backend that needs some closes over it at its registration site —
#: the callable signature stays ``() -> MessageBroker``.
BrokerFactory = Callable[[], MessageBroker]

#: A control-channel backend constructor — same no-config contract as
#: :data:`BrokerFactory`.
ControlChannelFactory = Callable[[], ControlChannel]


class BackendRegistry[G]:
    """name → factory for one service-level backend family.

    The service-level counterpart of the compile-time component slots
    (same role as ``ChannelAdapterRegistry``): backends resolve once per
    boot/per pool from config, never through scope compilation.
    Re-registering the same ``(name, factory)`` is idempotent; the same
    name with a different factory is a packaging error (loud).

    ``family`` labels the backend family in error messages (e.g.
    ``"message broker"``) so a boot failure names the family the
    configured name failed to resolve in.
    """

    def __init__(self, *, family: str) -> None:
        self._family = family
        self._factories: dict[str, Callable[[], G]] = {}

    def register(self, name: str, factory: Callable[[], G]) -> None:
        existing = self._factories.get(name)
        if existing is not None and existing is not factory:
            raise ValueError(
                f"{self._family} backend {name!r} registered twice with "
                "different factories (packaging/config error)"
            )
        self._factories[name] = factory

    def resolve(self, name: str) -> G:
        """Construct the backend registered under *name* (the
        config-resolution face — one call, one fresh backend instance)."""
        try:
            factory = self._factories[name]
        except KeyError:
            raise ValueError(
                f"{self._family} backend {name!r} is not registered — "
                f"registered backends: {list(self._factories)}; no plugin "
                "contributed it to the backend registry"
            ) from None
        return factory()

    def names(self) -> tuple[str, ...]:
        """Registered backend names, in registration order."""
        return tuple(self._factories)
