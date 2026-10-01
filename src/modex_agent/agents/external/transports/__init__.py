"""Transports — the external plane's access-form seam.

A transport drives the external coding agent for one turn and delivers
its observations as core :class:`~modex_agent.core.turn_events.TurnEvent`
records. The ABC (``abc.py``) carries the contract; ``cli_transport.py``
is the real CLI-subprocess transport (OpenCode); ``scripted.py`` is the
deterministic test double. Cross-transport concerns live in
:class:`~modex_agent.agents.external.normalizer.ExternalEventNormalizer`,
never in a transport.
"""

from .abc import (
    ChildTurnEventCallbackFactory,
    ExternalTransport,
    StaleSessionError,
    TurnEventCallback,
)
from .cli_transport import OpenCodeTransport
from .scripted import (
    ScriptedProgramme,
    ScriptedStep,
    ScriptedTransport,
    SendSideEffect,
)

__all__ = [
    "ChildTurnEventCallbackFactory",
    "ExternalTransport",
    "OpenCodeTransport",
    "ScriptedProgramme",
    "ScriptedStep",
    "ScriptedTransport",
    "SendSideEffect",
    "StaleSessionError",
    "TurnEventCallback",
]
