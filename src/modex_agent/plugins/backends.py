"""Service-level backend families — the broker/control-channel factory faces.

The service-level counterpart of the compile-time component slots (same
role as :class:`~modex_agent.plugins.loader.ChannelAdapterRegistry`):
infrastructure backends such as the message broker and the control
channel resolve once per boot from config, never through scope
compilation. Plugins contribute named backends through
:meth:`~modex_agent.plugins.loader.PluginRegistrationContext.register_broker`
/ ``register_control_channel``; the bundled defaults
(:mod:`modex_agent.plugins.defaults.backends`) register the
``in-memory`` pair.

The generic registry container itself lives in
:class:`modex_agent.core.backend_registry` (level 0) so the lowest
family owner — the protocol-engine registry under ``providers`` — can
import it downward; this module keeps only the two families' factory
type aliases (their ABCs import from level-1 packages, above core).

Registries are process-singletons created at boot (the app-service
registry load) — one instance per backend family.
"""

from __future__ import annotations

from collections.abc import Callable

from modex_agent.control.channel import ControlChannel
from modex_agent.messaging.broker import MessageBroker

__all__ = [
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
