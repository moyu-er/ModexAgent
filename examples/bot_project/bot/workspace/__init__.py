"""Business half of the workspace mechanism (pool-scoped resources).

The generic workspace mechanism lives in ``framework.workspace`` (registry,
resolver, paths, control). This package holds the business-specific resource
bundle — the concrete resource type ``R = PoolWorkspaceResources`` and its
factory, dispatcher, and BotService wiring. Pool is a business concept and
stays here; ``framework.workspace`` is pool-agnostic.
"""

from bot.workspace.background import BackgroundTaskRunner  # noqa: F401
from bot.workspace.dispatch import WorkspaceMessageDispatcher  # noqa: F401
from bot.workspace.factory import PoolResourceFactory  # noqa: F401
from bot.workspace.handle import (  # noqa: F401
    PoolWorkspaceResources,
    WorkspaceHandle,
    WorkspaceHandleRootProvider,
    WorkspaceResolverCell,
)
from bot.workspace.pool_data import PoolData, build_pool_data  # noqa: F401

# NOTE: ``wiring`` (build_workspace_stack) is intentionally NOT re-exported
# here — it imports ``bot.service.pool_builder`` (BotService), and this package
# is imported by tests/low-level modules that must not pull in the service
# layer. Import it explicitly: ``from bot.workspace.wiring import ...``.

__all__ = [
    "PoolWorkspaceResources",
    "WorkspaceHandle",
    "WorkspaceHandleRootProvider",
    "WorkspaceResolverCell",
    "PoolResourceFactory",
    "WorkspaceMessageDispatcher",
    "PoolData",
    "build_pool_data",
    "BackgroundTaskRunner",
]
