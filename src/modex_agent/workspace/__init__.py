"""Workspace mechanism (generic, business-agnostic).

The generic half of the workspace system: multi-live registry, lazy resource
materialization (generic over ``R``), path layout, and per-conversation control.
Business code plugs in a concrete resource type via
:class:`framework.workspace.factory.ResourceFactory` (see
``examples/bot_project/bot/workspace``).

Business-decoupled: this package imports only the standard library, ``typing``,
``abc``, and other ``framework.*`` modules — never ``bot.*``. Guarded by the
import-lint in ``tests/unit/workspace/test_isolation.py``.
"""

from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.factory import ResourceFactory
from modex_agent.workspace.models import CdError, CdResult
from modex_agent.workspace.paths import (
    RESERVED_GLOBAL_DIR,
    WorkspacePaths,
    is_reserved_segment,
    safe_segment,
)
from modex_agent.workspace.port import WorkspaceControlPort
from modex_agent.workspace.registry import (
    InMemoryRegistryStore,
    RegistryStore,
    WorkspaceRegistry,
)
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore

__all__ = [
    "RESERVED_GLOBAL_DIR",
    "ResourceFactory",
    "GlobalWorkspaceStore",
    "InMemoryRegistryStore",
    "RegistryStore",
    "WorkspaceContext",
    "WorkspaceController",
    "WorkspacePaths",
    "WorkspaceRegistry",
    "WorkspaceResolver",
    "WorkspaceControlPort",
    "CdError",
    "CdResult",
    "is_reserved_segment",
    "safe_segment",
]
