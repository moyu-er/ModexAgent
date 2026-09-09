"""ACP (Agent Client Protocol) adapter — editor-facing agent-server surface.

Public surface (ADR-0049, deliberately small): ``entry`` (stdio entry),
``server`` (``ModexAcpAgent``), the ``backend`` session seam, and the pure
mapper module ``events_map``. Import-light boundary: importing
``modex_agent.acp`` must NOT import the ``agent-client-protocol`` SDK (an
optional extra), so SDK-touching submodules (``server``, ``events_map``,
``entry``) are loaded lazily via module ``__getattr__``; the SDK-free
``types`` / ``backend`` / ``scripted`` modules load eagerly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from . import backend, types
from .backend import AcpInteraction, AcpSessionBackend, AcpSessionHandle
from .types import (
    AcpBackendError,
    AcpOpenKind,
    AcpOpenRequest,
    AcpPermissionOption,
    AcpPromptInput,
    AcpSessionMode,
    PermissionChoice,
    PermissionPrompt,
)

if TYPE_CHECKING:
    from . import entry, events_map, scripted, server

__all__ = [
    "AcpBackendError",
    "AcpInteraction",
    "AcpOpenKind",
    "AcpOpenRequest",
    "AcpPermissionOption",
    "AcpPromptInput",
    "AcpSessionBackend",
    "AcpSessionHandle",
    "AcpSessionMode",
    "PermissionChoice",
    "PermissionPrompt",
    "backend",
    "entry",
    "events_map",
    "scripted",
    "server",
    "types",
]

_LAZY_SUBMODULES = ("entry", "events_map", "scripted", "server")


def __getattr__(name: str) -> object:
    # Lazy optional-extra boundary (PEP 562): the SDK is only imported when an
    # SDK-touching submodule is actually requested.
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
